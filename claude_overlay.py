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
import hashlib
import ctypes.wintypes as wt
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import queue
from pathlib import Path

# Before anything that can fail: give a startup crash somewhere to go. The launcher runs
# this under pythonw, which has no console — so without a reporter installed first, every
# import below can kill the process with no window, no message and no log. That is the
# "I updated it, now double-clicking does nothing" bug: not a mystery, just an invisible
# ImportError. crashreport is stdlib-only precisely so it still works when the imports it
# is protecting are the broken thing.
try:
    import crashreport
except Exception as _boot_err:
    # crashreport.py itself is missing — which means this file was copied over an older
    # install on its own. That used to be the documented way to update from the ZIP, and
    # it stopped working when the app was split into modules: claude_overlay.py now needs
    # its siblings. Handled inline, with no imports beyond ctypes, because the reporter
    # that would normally say this is exactly the file that isn't there.
    _msg = ("Claude Overlay is missing part of itself (crashreport.py), so it can't "
            "start.\n\nThis happens when only claude_overlay.py was replaced during an "
            "update. The app is a folder of files now, not a single script.\n\nFix: "
            "re-download the whole folder — run update.cmd (git), or unzip ALL files "
            "from the latest ZIP over this one.\n\nFolder:\n"
            + os.path.dirname(os.path.abspath(__file__)))
    try:
        import ctypes as _ct
        _ct.windll.user32.MessageBoxW(None, _msg, "Claude Overlay", 0x10 | 0x10000)
    except Exception:
        pass
    try:
        sys.stderr and sys.stderr.write(_msg + "\n")
    except Exception:
        pass
    raise SystemExit(1)

crashreport.install()


def _report_import_failure(exc):
    """Turn a failed startup import into a message that names the fix.

    The raw traceback is necessary but not sufficient: "ModuleNotFoundError: PIL" tells
    a developer everything and a colleague nothing. preflight turns it into which
    interpreter is running, what's missing from it, and the one command that repairs it —
    so the dialog is actionable without a round trip to whoever maintains this."""
    detail = ""
    try:
        import preflight
        detail = preflight.summary(preflight.check()) + "\n\n"
    except Exception:
        pass
    # Ahead of the traceback, because the dialog is capped and read top-down: the person
    # seeing it is not the person who can use a stack trace, and the per-problem pip
    # commands above still assume a terminal. The two double-clickable files repair the
    # environment AND prove the app loads before claiming success — so name them first.
    detail += ("Quickest fix: double-click update.cmd (or setup.cmd) in the app folder - "
               "either one reinstalls what is missing into the Python the launcher "
               "actually runs, and verifies the app loads before reporting success.\n\n")
    try:
        import traceback as _tb
        detail += "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        detail += repr(exc)
    crashreport.report(
        f"Claude Overlay couldn't start: {exc.__class__.__name__}: {exc}", detail,
        app_version=_file_version())
    # As the launched app there is nothing left to do but say so and leave. When merely
    # IMPORTED (CI's import smoke, the tests, preflight's subprocess check) the exception
    # must keep propagating — swallowing it would turn a red build green.
    if __name__ == "__main__":
        os._exit(1)
    raise exc


def _file_version():
    """config.__version__ without importing config — config may be what failed."""
    try:
        import re as _re
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py"),
                   encoding="utf-8").read()
        m = _re.search(r'^__version__\s*=\s*"([^"]+)"', src, _re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


try:
    import tkinter as tk
    from tkinter import font as tkfont

    from PIL import Image, ImageGrab, ImageDraw, ImageChops, ImageFilter, ImageTk

    from config import *
    from config import __version__
    from debuglog import dbg, DEBUG_LOG
    from win32utils import *
    from win32utils import _user32, _gdi32
    from worker import ClaudeWorker
    import authstate
    import sessions
    import usage
except Exception as _e:
    _report_import_failure(_e)

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


# ── predicting how long a /compact will take ──────────────────────────────────
# The CLI streams NOTHING while it compacts (see COMPACT_IDLE_TIMEOUT) — no percentage, no
# stage events — so real progress is unknowable. What IS knowable is how long compactions
# of a given size have taken before: every one leaves a `compact_boundary` line carrying
# compactMetadata.preTokens + durationMs. We remember our own runs in STATE_FILE and, on a
# machine that hasn't compacted through the overlay yet, mine the CLI's transcripts, so
# even the first run can show an honest estimate instead of an unlabelled spinner.
_COMPACT_HIST_MAX = 10            # rolling window of remembered runs
_COMPACT_SCAN_FILES = 12          # newest transcripts only, when seeding from the CLI's logs
_COMPACT_ETA_MIN = 10.0
_COMPACT_ETA_MAX = 900.0
_COMPACT_ETA_Q = 0.80             # aim the estimate here, not at the middle (see _compact_predict)
_COMPACT_ETA_PAD = 0.15           # …and never less than this much headroom, however tight the fit


def _compact_history():
    """Remembered (pre_tokens, duration_sec) pairs from this overlay's own compactions."""
    raw = _load_state().get("compact_runs")
    out = []
    if isinstance(raw, list):
        for it in raw[-_COMPACT_HIST_MAX:]:
            try:
                pre, dur = int(it[0]), float(it[1])
            except Exception:
                continue          # hand-edited / older-format entry — just skip it
            if pre > 0 and dur > 0:
                out.append((pre, dur))
    return out


def _compact_history_add(pre_tokens, duration_sec):
    """Append one completed run, keeping only the most recent _COMPACT_HIST_MAX."""
    try:
        pre, dur = int(pre_tokens), float(duration_sec)
    except Exception:
        return
    if pre <= 0 or dur <= 0:
        return
    runs = _compact_history() + [(pre, round(dur, 1))]
    _save_state(compact_runs=[list(r) for r in runs[-_COMPACT_HIST_MAX:]])


def _compact_samples_from_transcripts():
    """(pre_tokens, duration_sec) pairs recovered from the CLI's own session logs. Purely
    best-effort — an unreadable file or a malformed line is skipped, [] on any failure."""
    out = []
    try:
        files = sorted(sessions.TRANSCRIPT_ROOT.glob("*/*.jsonl"),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:_COMPACT_SCAN_FILES]
    except Exception:
        return out
    for f in files:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if "compact_boundary" not in ln:   # cheap reject before the JSON parse
                        continue
                    try:
                        md = json.loads(ln).get("compactMetadata") or {}
                    except Exception:
                        continue
                    pre, ms = md.get("preTokens"), md.get("durationMs")
                    if isinstance(pre, (int, float)) and isinstance(ms, (int, float))                             and pre > 0 and ms > 0:
                        out.append((int(pre), ms / 1000.0))
        except Exception:
            continue              # log rotated away mid-read, permissions, etc.
    return out


def _compact_quantile(values, q):
    """Linearly-interpolated quantile. statistics.quantiles() wants n ≥ 2 and returns cut
    points between groups; here a single sample has to work too, so do it by hand."""
    vs = sorted(values)
    if not vs:
        return 0.0
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo = min(int(pos), len(vs) - 2)
    return vs[lo] + (vs[lo + 1] - vs[lo]) * (pos - lo)


def _compact_predict(samples, pre_tokens):
    """Predicted /compact duration in seconds, or None when there's nothing to go on.

    Measured runs (61k tokens → 96s, 283k → 167s) say duration is mostly a FIXED cost plus a
    small per-token term, so scaling straight off the context size would badly underestimate
    small compactions. With two or more differently-sized samples we least-squares fit
    duration = a + b·tokens; otherwise we fall back to the median duration, which ignores
    size but still beats having no estimate at all.

    Both of those are CENTRE estimates, which by construction half of all runs overshoot —
    and overshooting reads far worse than finishing early, because the bar stalls with no way
    to say how much longer. So aim at the _COMPACT_ETA_Q quantile instead: pad the fit by the
    spread of its own residuals, the median by the spread of the durations. Two samples make
    the fit pass exactly through both points and three make it nearly so, so a residual-based
    pad alone would be ~0 exactly when confidence is lowest — hence the _COMPACT_ETA_PAD floor."""
    pts = [(p, d) for p, d in samples if p > 0 and d > 0]
    if not pts:
        return None
    clamp = lambda v: min(_COMPACT_ETA_MAX, max(_COMPACT_ETA_MIN, v))
    pad = lambda base, spread: clamp(base + max(spread, base * _COMPACT_ETA_PAD))
    if isinstance(pre_tokens, (int, float)) and pre_tokens > 0 and len(pts) >= 2:
        xs, ys = [p for p, _ in pts], [d for _, d in pts]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        var = sum((x - mx) ** 2 for x in xs)
        if var > 0:               # zero ⇒ every sample the same size, no slope to fit
            b = sum((x - mx) * (y - my) for x, y in pts) / var
            a = my - b * mx
            resid = [y - (a + b * x) for x, y in pts]
            return pad(a + b * pre_tokens, _compact_quantile(resid, _COMPACT_ETA_Q))
    ds = [d for _, d in pts]
    med = statistics.median(ds)
    return pad(med, _compact_quantile(ds, _COMPACT_ETA_Q) - med)


# ── telling two screenshots apart ─────────────────────────────────────────────
# Auto-shot attaches a capture to EVERY message, and each one costs ~1.5-2.5k vision tokens
# that then sit in the context for the rest of the conversation — on a long session the
# screenshots, not the words, are what fills the window. Byte-equality is nearly useless
# against that: four consecutive grabs of a screen nobody touched produced four different
# sha256s (measured 2026-08 — a live desktop is never bit-for-bit still), so the cheap test
# almost never fires and every message pays for a picture the model already has. So compare
# what the capture LOOKS like instead, with a difference hash over a luma grid — each bit
# says "this cell is brighter than the one to its right". At 32 cells wide a caret or a clock
# digit averages away to far less than one cell (the same four grabs were 0 bits apart),
# while anything worth re-sending moves whole regions and flips bits by the dozen. A finer
# grid doesn't sharpen the distinction — 48 and 64 wide were measured too, and a localized
# change stays localized at every resolution — so the coarsest one wins on cost.
_SHOT_HASH_SIDE = 32              # 32×32 left-to-right comparisons = 1024 bits of fingerprint


def _shot_phash(path):
    """1024-bit perceptual fingerprint of an image file, or None if it can't be read.

    None means "no opinion", never "unchanged": callers fall back to byte-equality rather
    than guess, because a wrong "unchanged" points the model at an image it was never sent."""
    try:
        with Image.open(path) as im:
            # BOX = plain area averaging. A sharper filter (LANCZOS) rings around edges and
            # would let a one-pixel caret swing a whole cell — the opposite of what's wanted.
            g = im.convert("L").resize((_SHOT_HASH_SIDE + 1, _SHOT_HASH_SIDE), Image.BOX)
        px = g.tobytes()          # mode "L" → one unpadded byte per pixel, row-major
        w = _SHOT_HASH_SIDE + 1
        bits = 0
        for y in range(_SHOT_HASH_SIDE):
            row = y * w
            for x in range(_SHOT_HASH_SIDE):
                bits = (bits << 1) | (px[row + x] > px[row + x + 1])
        return bits
    except Exception:
        return None


def _shot_looks_same(a, b):
    """True when two fingerprints differ by at most SHOT_DEDUPE_BITS of the 1024."""
    if a is None or b is None or SHOT_DEDUPE_BITS <= 0:
        return False
    return bin(a ^ b).count("1") <= SHOT_DEDUPE_BITS


# ── how fast the context window is being spent ────────────────────────────────
# A bare "context 46%" answers a question nobody asks. What people want to know is whether
# they can keep going, and the unit they spend is the turn — so track where the window stood
# at the end of each recent turn and quote the slope in turns remaining.
_CTX_RATE_TURNS = 6               # rolling window the burn rate is averaged over
_CTX_WARN_PCT = 70.0              # amber, and a one-time note suggesting an early compaction
_CTX_HOT_PCT = 85.0               # red, and a second, louder note


# ── how much of the allowance is left ─────────────────────────────────────────
# The gauge above measures the size of the CONVERSATION. What actually ends a session is the
# 5-hour/weekly allowance, and the two are unrelated: every message spends from the
# allowance, but Clear and /compact hand the context reading back while the spend stays
# spent. So a window that never rises past a few percent can sit next to an allowance that's
# nearly gone — which is precisely how you get cut off with no warning, and why the context
# number can't be the one on display. The CLI knows the real figure and emits it on every
# status transition (RateLimitEvent → the worker's "quota"); the statusline gives it the slot
# and lends it back to context only when context is itself over its warning line, so the row
# never carries two percentages competing to be the one you should worry about.
_QUOTA_WINDOWS = {"five_hour": "5h", "seven_day": "week", "seven_day_opus": "week/opus",
                  "seven_day_sonnet": "week/sonnet", "overage": "overage"}
_QUOTA_HOT = 0.90                 # colour by the number shown, even if the CLI still says
                                  # "allowed" — a grey 94% reads as nothing being wrong
_QUOTA_WARN = 0.75                # the ring's amber step. The text gauge can wait for the
                                  # CLI's own "allowed_warning" because it prints a number you
                                  # read; an arc has no number, so it has to earn attention
                                  # before it is nearly spent or it says nothing until too late.


def _mix(a, b, t):
    """Blend two #rrggbb colours, t of the way from a to b."""
    x, y = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(round(p + (q - p) * t) for p, q in zip(x, y))


def _contrast(a, b):
    """WCAG contrast ratio between two #rrggbb colours — used to prove a gauge is visible
    rather than to assume it, after a track at 1.2:1 shipped as a blank mark."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    p, q = lum(a), lum(b)
    return (max(p, q) + 0.05) / (min(p, q) + 0.05)


# Radii and stroke widths as fractions of a 32px mark, the size they were tuned at; the mark
# is drawn at _MARK_PX and these scale with it. Inner track is the 5-hour window and is the
# thicker of the two — it is the one that ends the session you are sitting in.
_RING_GEOM = (("five_hour", 10.5 / 32, 3 / 32), ("week", 14.5 / 32, 2 / 32))
_SPARK_R = 5.75 / 32           # small enough to leave clear air inside the 5h track:
                               # when that arc goes amber it is T["accent"], the same
                               # colour as the mark, and touching arc and mark merge
_RING_SS = 4                      # supersample factor, then downsample: Tk's create_arc has
                                  # no antialiasing on Windows and a 3px arc on a 36px circle
                                  # comes out a visible staircase
_MARK_PX = 36                     # two legible arcs plus a readable ✻ need this much room;
                                  # the titlebar is 44px tall, so this is the largest that fits


def _binding_window(windows):
    """The one window that earns the statusline's single text slot: whichever is furthest
    along, because the binding constraint is the limit you reach first and the rest are noise
    until they overtake it. Ties go to five_hour — at equal percentages it is the one that can
    end the session you are sitting in right now.

    This rule used to live in usage.reading(), which applied it before the UI ever saw the
    data. That was the wrong altitude twice over: it is a decision about what to SHOW, and
    collapsing to it in the data layer threw away the other windows — including the 5-hour one
    the ring now draws, which by definition is the one being discarded during the whole early
    stretch when it sits below the weekly number. Reporting is usage.py's job; choosing is ours.

    Takes windows keyed by name (utilization already 0-1) and returns one of them with its
    name folded in, or None. The result deliberately carries no `status`: a polled reading has
    none, so _gauge_color goes on colouring by the number, which is what it already did.
    """
    best = None
    for name in _QUOTA_WINDOWS:
        w = (windows or {}).get(name)
        if not isinstance(w, dict):
            continue
        u = w.get("utilization")
        if isinstance(u, bool) or not isinstance(u, (int, float)):
            continue
        if best is None or u > best[1]:
            best = (name, float(u))
    return dict(windows[best[0]], window=best[0]) if best else None


_RETRY_POLL_MS = 60_000           # how often an armed retry checks the clock. A single long
                                  # after() would be the obvious choice and the wrong one: Tk
                                  # timers don't run while the machine sleeps, so a laptop
                                  # closed for the afternoon would wake owing hours of delay.
                                  # Re-reading the wall clock can't drift that way.


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
        self._sent_shot_hashes: dict = {}  # capture-target key → (sha256, perceptual hash) of
                                        # the last shot the model VERIFIABLY has in context;
                                        # lets auto-screenshot skip re-attaching a screen that
                                        # hasn't visibly changed (see _dedupe_shots). Cleared
                                        # whenever the context may have lost the image
                                        # (clear/compact — explicit OR the CLI's automatic one
                                        # — reconnect, resume fallback, any error).
        self._pending_shot_hashes: dict = {}  # hashes of shots attached to the IN-FLIGHT turn;
                                        # promoted into _sent_shot_hashes only when that turn
                                        # returns a clean result. A turn that errors or is
                                        # stopped may never have shown the image to the model —
                                        # committing eagerly would make the next send dedupe
                                        # against an image the model never saw.
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
        self._ctx_tokens = None         # absolute context size, for pricing a would-be /compact
        self._ctx_hist: list = []       # context % at the end of each of the last few turns →
                                        # burn rate → how many turns of headroom are left
        self._ctx_warned = 0.0          # highest warning tier already announced; reset with the
                                        # conversation and after a compaction wins the room back
        self._ctx_sample_due = False    # a turn ended; the next usage refresh is its data point
        self._quota = None              # last rate-limit reading the CLI reported (see "quota")
        self._quota_polled = None       # last reading usage.py fetched itself (see "quota_poll").
                                        # Kept apart from _quota, not merged into it: this one is
                                        # fresher and so wins the DISPLAY, while everything that
                                        # speaks or sends still reads the CLI's own event above.
        self._quota_said = None         # status already announced, so each transition speaks once
        self._ring_explained = False    # the ring names itself once, when there is finally
                                        # something to point at (see _maybe_explain_ring)
        self._last_sent = None          # (text, images) of the last message handed to the worker,
                                        # so a refusal that never reached Claude can give it back
        self._retry = None              # {"at", "text", "armed"} — a refused message waiting for
                                        # the allowance to come back (see _offer_retry)
        self._retry_btn = None          # the in-chat arm/cancel button for it
        self._retry_after = None        # pending after() id for the poll tick
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
        # Each user bubble owns its own canvas, and canvas text selection is per-canvas, so
        # nothing clears bubble A when you start selecting in bubble B. Track the bubble that
        # currently holds a selection and clear it by hand, or old highlights pile up.
        self._sel_bubble = None
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
        self._ov_update_shown = False     # show the "a newer overlay exists" notice at most once
        self._ov_update_btn_ref = None    # the in-chat Update-overlay button, same restyle path
        self._session_id = None           # the CLI's id for the current conversation (worker
                                          # events); persisted per completed turn so the NEXT
                                          # launch can offer to resume — see _persist_session
        self._resume_btn = None           # the in-chat "Resume last conversation" button while
                                          # it's still actionable, so events can restyle it
        self._discard_pending = False     # True from a Clear click until the worker's reset_done
                                          # drains: a turn's (session / turn_done) batch already
                                          # queued before the click must not re-set _session_id or
                                          # re-persist the record and resurrect the discarded chat
        self._restarting = False          # guard: one self-restart (relaunch + quit) at a time
        self._mapping = False             # re-entrancy guard for the <Map> taskbar re-assert
        self._fronting = False            # re-entrancy guard for _raise_to_front (focus churn)
        self._vscreen_sig = None          # last virtual-desktop bounding box (display-topology sig)
        self._vscreen_checked = 0.0       # throttle the topology watchdog to ~1.5s in _poll
        self._last_ext_fg = None          # last EXTERNAL foreground hwnd — the window the user was
                                          # working in before focusing the overlay; the "window"
                                          # capture scope targets it whenever the overlay has focus
        self._fg_checked = 0.0            # throttle that tracking to ~0.5s in _poll
        # The `claude` CLI's login can die in a way NOTHING here can repair: a refresh
        # rejected with invalid_grant makes the CLI blank its own stored credentials, after
        # which every message fails until the user signs in again (see authstate.py). These
        # track that state so it's announced once — not per message — and so a send isn't
        # allowed to consume a typed prompt and its attachments into a turn that cannot work.
        self._auth_dead = False           # last known "stored login is provably unusable"
        self._auth_checked = 0.0          # throttle the credential watchdog in _poll
        self._auth_told = False           # the notice has been shown for the CURRENT death
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
        self._compact_pre = None          # context size (tokens) this run is compacting
        self._compact_eta = None          # predicted duration in s; None → no basis to guess

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
        # Status-bar icon font: the native Windows symbol face renders a crisp, monochrome
        # settings gear (U+E713 — the same glyph Windows uses in its own Settings UI) that
        # colors cleanly with the theme, far nicer than the thin U+2699 the body font makes.
        # Falls back to that plain glyph if neither Windows icon font is present.
        _icon_fam = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in avail), None)
        if _icon_fam:
            self.f_icon, self.gear_glyph = mk(_icon_fam, 13), "\uE713"
        else:
            self.f_icon, self.gear_glyph = self.f_small, "⚙"

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
        self._maybe_offer_resume()

        self.root.after(130, lambda: (self.root.focus_force(), self.entry.focus_set()))
        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<Map>", self._on_map, add="+")   # restore (incl. from taskbar) re-asserts the frameless look
        self.root.bind("<FocusIn>", self._on_focus_in, add="+")  # taskbar-click / alt-tab activation → raise above topmost peers
        self.root.after(170, self._apply_region)
        self.root.after(180, self._apply_share_visibility)
        self.root.after(220, self._install_taskbar_button)
        self.root.after(1200, self._check_for_update)
        self.root.after(1500, self._check_cli_update)
        self._usage_poll = usage.Poller(self.ui_q)
        self._start_usage_poll()
        self._start_hang_watchdog()    # diagnostic: dumps all-thread stacks if the UI pump stalls

    def _start_usage_poll(self):
        """Start the allowance poll, so the gauge has a number before the first message.

        Its own method purely so the test suite can neuter it on the Overlay it builds
        (no network, and this machine's real token never read) WITHOUT patching
        usage.Poller itself — the session-wide Overlay fixture would otherwise leave the
        class stubbed for every later unit test of that class."""
        self._usage_poll.start()

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
        sz = self.px(_MARK_PX)
        mark = tk.Canvas(bar, width=sz, height=sz, bg=T["bg"], highlightthickness=0)
        mark.pack(side="left", padx=(self.px(10), self.px(7)))
        # The whole mark — allowance arcs AND the ✻ — is one supersampled image rather than
        # Tk canvas primitives, so every curve is antialiased. See _paint_quota_ring.
        self._mark, self._mark_sz = mark, sz
        self._paint_quota_ring()
        # The ring can carry the numbers but not their name. A pointer cursor says it answers
        # to something, and <Enter> is where the answer arrives — see _quota_hover_text.
        mark.configure(cursor="hand2")
        mark.bind("<Enter>", self._mark_enter, add="+")
        mark.bind("<Leave>", self._mark_leave, add="+")
        # Deliberately a child of root rather than a Toplevel. The capture exclusion that keeps
        # the overlay out of screen shares - and out of the screenshots we send Claude - is set
        # on the root HWND (see _apply_share_visibility) and a new top-level window does not
        # inherit it: the panel would show up in a Teams share while the overlay itself did not,
        # and would land inside our own grabs, because capture() skips its withdraw dance
        # whenever the exclusion is active. A placed child inherits all of that for nothing.
        self._usage_panel = tk.Label(self.root, text="", bg=T["tool_bg"], fg=T["text"],
                                     font=self.f_mono, justify="left", anchor="w",
                                     padx=self.px(10), pady=self.px(8),
                                     highlightthickness=1, highlightbackground=T["border"])
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
        # Scroll-follow state. Following the end is a MODE the user turns off by scrolling up
        # (and back on by scrolling to the end), not something re-derived from the view on every
        # insert - see _sync_follow.
        self._follow = True
        self._unread = False
        self._jump = None
        self._jump_shown = False
        self._jump_unread_drawn = None
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
        # Starting a selection in the transcript drops any user-bubble highlight, so the window
        # never shows two selections at once. add="+" so Text's own click bindings still run.
        self.chat.bind("<Button-1>", lambda e: self._bubble_sel_clear(), add="+")
        self.chat.bind("<Configure>", self._on_chat_configure, add="+")
        self._build_jump()

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
        self._update_jump()      # the view moved -> the jump pill may need to appear/disappear

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
        self._sync_follow()
        self._sb_redraw()

    def _sb_motion(self, e):
        if self._sb_drag is None:
            return
        h = self.scrollbar.winfo_height()
        if h <= 1:
            return
        self.chat.yview_moveto(max(0.0, min(1.0, (e.y - self._sb_drag) / h)))
        self._sync_follow()

    # -- scroll-follow + "jump to latest" --
    def _at_bottom(self):
        try:
            return self.chat.yview()[1] > 0.999
        except Exception:
            return True

    def _sync_follow(self):
        """Re-derive follow from the view after a USER scroll gesture (wheel, scrollbar drag,
        keyboard): scrolled up -> stop following, scrolled back to the end -> resume. This is the
        ONLY thing that turns following off, so content-driven drift (a throttled giant line, an
        embedded table, a resize) can no longer strand a reply below the fold."""
        self._follow = self._at_bottom()
        if self._follow:
            self._unread = False
        self._update_jump()

    def _scroll_follow(self):
        """Called after appending content: keep the end in view while following, otherwise note
        that unseen output arrived so the jump pill can say so."""
        if self._follow:
            try:
                self.chat.see("end")
            except Exception:
                pass
        else:
            self._unread = True
            self._update_jump()

    def _jump_to_end(self, e=None):
        self._follow = True
        self._unread = False
        try:
            self.chat.see("end")
        except Exception:
            pass
        self._update_jump()
        return "break"

    def _build_jump(self):
        """A small pill floated over the bottom-right of the chat, visible only once you have
        scrolled away from the end. Without it, output streaming in below the fold gives no sign
        at all that it arrived."""
        self._jump = tk.Canvas(self.chat_wrap, bg=T["bg"], highlightthickness=0,
                               cursor="hand2", takefocus=0)
        self._jump.bind("<Button-1>", self._jump_to_end)
        self._jump.bind("<MouseWheel>", self._fwd_wheel)   # must not swallow the scroll

    def _render_jump(self):
        cv = self._jump
        if cv is None or not self._widget_alive(cv):
            return
        txt = "\u2193 New output" if self._unread else "\u2193 Latest"
        f = self.f_chip
        w = f.measure(txt) + self.px(22)
        h = f.metrics("linespace") + self.px(10)
        cv.configure(width=w, height=h)
        cv.delete("all")
        bg = T["accent"] if self._unread else T["tool_bg"]
        fg = T["bg"] if self._unread else T["text"]
        round_rect(cv, 0, 0, w - 1, h - 1, (h - 1) / 2, fill=bg, outline="")
        cv.create_text(w / 2, h / 2, text=txt, fill=fg, font=f)

    def _update_jump(self):
        """Show/hide the pill. Uses the cached scrollbar fraction (free - yscrollcommand already
        handed it to us) rather than yview(), which is O(line length) on a giant streamed line."""
        cv = getattr(self, "_jump", None)
        if cv is None:
            return
        want = not self._follow and self._sb_last < 0.999
        try:
            if want:
                if not self._jump_shown or self._jump_unread_drawn != self._unread:
                    self._render_jump()
                    self._jump_unread_drawn = self._unread
                if not self._jump_shown:
                    cv.place(relx=1.0, rely=1.0, anchor="se",
                             x=-(self._sb_w + self.px(14)), y=-self.px(10))
            elif self._jump_shown:
                cv.place_forget()
        except Exception:
            pass
        self._jump_shown = want

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
        self.gear = tk.Label(st, text=self.gear_glyph, bg=T["bg"], font=self.f_icon, cursor="hand2")
        self.gear.pack(side="left", padx=(self.px(10), self.px(2)), pady=pad)
        self.gear.bind("<Button-1>", self._gear_menu)
        self.gear.bind("<Enter>", lambda e: self.gear.configure(fg=T["accent"]))
        self.gear.bind("<Leave>", lambda e: self._paint_gear())
        self._paint_gear()
        self.attach_lbl = tk.Label(st, text="", bg=T["bg"], fg=T["accent"],
                                   font=self.f_small, cursor="hand2")
        self.attach_lbl.pack(side="left", padx=self.px(6), pady=pad)
        self.attach_lbl.bind("<Button-1>", lambda e: self._clear_attachments())
        # Mode chips sit between the ⚙ and the attachment label (hence `before=` in
        # _paint_modes). One per setting that is NOT at its quiet default, so the strip is
        # empty on a stock overlay and anything visible means "this one is behaving
        # differently" — the reason the old always-on inline toggles were removed was that
        # they crowded the bar even when they had nothing to say. The ⚙ menu still spells
        # all three out with checkmarks; clicking a chip opens it.
        self.mode_lbls = {}
        for key in self.MODE_CHIPS:
            lbl = tk.Label(st, bg=T["bg"], font=self.f_small, cursor="hand2")
            lbl.bind("<Button-1>", self._gear_menu)
            self.mode_lbls[key] = lbl
        # Resizing changes whether the spelled-out chips still fit, so re-decide on every
        # layout pass. add="+" leaves any other <Configure> handler on the bar intact.
        st.bind("<Configure>", self._paint_modes, add="+")
        self._paint_modes()
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
        self.statusline.pack(side="left", padx=(self.px(16), 0), pady=(0, self.px(6)))
        self.statusline.bind("<Button-1>", self._model_menu)
        # The context gauge is its OWN label so it can go amber/red on its own: recolouring one
        # line to warn about one number would have dragged the model name and version with it.
        # It also keeps the model menu's click target on the model, where it belongs.
        self.ctx_lbl = tk.Label(sl, text="", bg=T["bg"], fg=T["faint"],
                                font=self.f_small, anchor="w")
        self.ctx_lbl.pack(side="left", padx=(self.px(9), 0), pady=(0, self.px(6)))
        self.ver_lbl = tk.Label(sl, text="", bg=T["bg"], fg=T["faint"],
                                font=self.f_small, anchor="w")
        self.ver_lbl.pack(side="left", padx=(self.px(9), self.px(6)), pady=(0, self.px(6)))
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
        self._paint_modes()

    # Chip text + colour + the value worth showing, per mode attribute. The "worth showing"
    # value is the NON-default one in each case: Read-only locks the session, Window-only
    # narrows what gets captured, and Shareable means the overlay is visible in screen shares
    # (stock is excluded — see SHOW_IN_SCREEN_SHARE_DEFAULT). Read-only takes the accent
    # because it is the safety state, matching what the gear colour already signals.
    MODE_CHIPS = {
        "read_only":     ("⊘", "Read-only", "accent", True),
        "window_shot":   ("▣", "Window",    "muted",  True),
        "share_visible": ("◈", "Shared",    "muted",  True),
    }

    def _modes_fit(self, labels):
        """Is there room on the bar for the spelled-out chips, or must they go glyph-only?
        Overlays get kept narrow (all three chips spelled out want ~1.5x the default width),
        and a clipped chip is worse than a terse one: pack just drops it off the edge, so the
        mode would go from mislabelled to invisible. Measured against everything else already
        on the bar. No feedback loop — the mode labels are excluded from `used`, so the answer
        can't change as a result of acting on it."""
        st = getattr(self, "status_frame", None)
        avail = st.winfo_width() if st is not None else 0
        if avail <= 1:
            return True          # not laid out yet; <Configure> re-runs this once it is
        mine = set(self.mode_lbls.values())
        used = sum(w.winfo_reqwidth() + self.px(12) for w in st.pack_slaves() if w not in mine)
        need = sum(self.f_small.measure(t) + self.px(10) for t in labels)
        return used + need <= avail

    def _paint_modes(self, _e=None):
        """Re-pack the mode strip from current state. Everything is unpacked and re-packed in
        MODE_CHIPS order rather than toggled individually, so the chips keep a stable
        left-to-right order however they were switched on. Guarded so the paint helpers stay
        safe to call before the status bar exists / in headless tests."""
        if not getattr(self, "mode_lbls", None):
            return
        on = [k for k in self.MODE_CHIPS
              if getattr(self, k, None) == self.MODE_CHIPS[k][3]]
        full = self._modes_fit([f"{self.MODE_CHIPS[k][0]} {self.MODE_CHIPS[k][1]}" for k in on])
        for lbl in self.mode_lbls.values():
            lbl.pack_forget()
        for key in on:
            glyph, label, colour, _ = self.MODE_CHIPS[key]
            self.mode_lbls[key].configure(text=(f"{glyph} {label}" if full else glyph),
                                          fg=T[colour])
            self.mode_lbls[key].pack(side="left", padx=(self.px(6), 0), pady=self.px(4),
                                     before=self.attach_lbl)

    def _active_modes(self):
        """The chip texts currently on the bar, in bar order. Split out so a test can assert on
        what is shown without reaching into Tk's pack internals."""
        return [self.mode_lbls[k].cget("text") for k in self.MODE_CHIPS
                if getattr(self, k, None) == self.MODE_CHIPS[k][3]]

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
            ("      Past conversations…", self.show_sessions),
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
        self._jump_unread_drawn = None      # font scaled -> the pill must be re-measured
        self._update_jump()

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
            self.root.after_idle(self._sync_follow)   # keyboard scrolling is a user gesture too
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
        self.chat.insert("end", text, tags)
        self._scroll_follow()
        self._prune_chat()

    def add_user(self, text):
        self._md_finalize()              # commit the previous turn's last line before a new bubble
        self._turn_raw = ""              # a new turn starts → fresh assistant-answer buffer
        self._turn_copy_added = False
        self._set_task_badge(False)      # a new task → clear any stale "done" badge on the orb
        self._follow = True              # you just sent something → follow the reply again
        self._unread = False
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._user_bubble(text), pady=self.px(3))
        self.chat.insert("end", "\n")
        try:
            self.chat.tag_remove("current_ah", "1.0", "end")   # a new turn starts; old header
        except Exception:                                       # is no longer the "active" one
            pass
        self._claude_header = False
        self._thinking_active = False    # new turn → next thinking re-inserts its label
        self._scroll_follow()
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
        a stale fixed size (the reason this used to be frozen). Registered with _register_zoomable.

        Click-to-copy: the bubble is a Canvas, so the text in it is *drawn*, not text — Tk has no
        selection model for canvas items, which is why you can never drag-select your own message
        the way you can Claude's (that side is real text in the Text widget). Clicking the bubble
        copies it instead. It copies `raw`, captured BEFORE _clip_bubble, because the echo you see
        is lossy on purpose: truncated at 2000 chars and with spaces injected into long unbroken
        runs to keep canvas wrapping linear. Copying what's drawn would hand back mangled text."""
        raw = "" if text is None else str(text)   # pre-clip original → this is what the clipboard gets
        shown = self._clip_bubble(text)
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="xterm", takefocus=0,
                      selectbackground=T["accent"], selectforeground=T["on_accent"])
        c._copied = False
        c._shown = shown        # a partial selection yields a slice of THIS, i.e. of what is drawn
        c._item = None          # the text item; selection and index lookups both target it
        c._sel = None           # (anchor, last) inclusive char indices, kept across a re-render
        st = {"hover": False, "anchor": None, "moved": False}

        def paint():
            """Hover / '✓ Copied' feedback WITHOUT a full redraw. render() starts with
            delete("all"), which drops the canvas text selection — and hover fires while you
            are mid-drag, so repainting that way would erase the selection as you made it."""
            lit = c._copied or st["hover"]
            try:
                c.itemconfigure(c._rect, fill=T["sel"] if lit else T["user_card"])
                if c._hint is not None:
                    c.itemconfigure(c._hint, text="✓ Copied" if c._copied else "⧉ Copy",
                                    fill=T["accent"] if c._copied else T["faint"],
                                    state="normal" if lit else "hidden")
            except Exception:
                pass

        def render():
            keep = c._sel                                   # zoom/resize must not lose a selection
            c.delete("all")
            full = max(self.px(200), self.chat.winfo_width() - 2 * self.px(18))
            maxw = max(self.px(140), int(full * 0.74))
            padx, pady, rad = self.px(13), self.px(9), self.px(14)
            body_font = tkfont.Font(root=self.root, font=self.f_body)   # current zoom
            hint_font = tkfont.Font(root=self.root, font=self.f_small)
            c._overlay_fonts = [body_font, hint_font]       # keep refs so Tk won't GC them
            tmp = c.create_text(0, 0, text=shown, font=body_font, width=maxw, anchor="nw")
            bb = c.bbox(tmp)
            x1, y1, x2, y2 = bb if bb else (0, 0, maxw, self.px(18))
            c.delete(tmp)
            bw, bh = (x2 - x1) + 2 * padx, (y2 - y1) + 2 * pady
            bx = full - bw                                  # hug the right edge
            c._rect = round_rect(c, bx, 1, bx + bw, bh - 1, rad, fill=T["user_card"], outline="")
            c._item = c.create_text(bx + padx, pady, text=shown, font=body_font, fill=T["text"],
                                    width=maxw, anchor="nw")
            # Affordance sits in the gutter LEFT of the bubble (which hugs the right edge).
            # maxw caps the bubble at 74% of the width, so there is normally room; when a short
            # window leaves none, the fill change alone carries the feedback rather than drawing
            # a label over the bubble's own corner. Sized to the WIDER label so it never reflows.
            c._hint = None
            if bx - self.px(8) >= hint_font.measure("✓ Copied"):
                c._hint = c.create_text(bx - self.px(8), pady + body_font.metrics("linespace") / 2,
                                        text="⧉ Copy", font=hint_font, anchor="e",
                                        fill=T["faint"], state="hidden")
            c.configure(width=full, height=bh)
            paint()
            if keep:
                select_chars(*keep)

        def select_chars(a, b):
            """Select shown[a:b+1] (Tk canvas selection is inclusive at both ends). Also the
            seam tests use, because Tk drops synthesised drag events on the withdrawn widgets
            this suite runs on, so a test cannot produce a selection by faking the mouse."""
            if c._item is None:
                return
            try:
                c.select_from(c._item, a)
                c.select_to(c._item, b)
            except Exception:
                return
            c._sel = (a, b)
            self._sel_bubble = c

        def sel_text():
            if not c._sel:
                return None
            a, b = sorted(c._sel)
            return shown[a:b + 1] or None

        def restore():
            try:
                c._copied = False
                paint()
            except Exception:
                pass

        def copy(payload):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(payload)
            except Exception:
                pass
            c._copied = True
            paint()                    # paint() honours _copied → swaps in the '✓ Copied' state
            try:
                c.after(1200, restore)
            except Exception:
                pass

        def hit(e):
            return c.index(c._item, "@%d,%d" % (int(c.canvasx(e.x)), int(c.canvasy(e.y))))

        def on_press(e):
            self._bubble_sel_clear(keep=c)       # only one bubble may show a selection at a time
            try:
                c.select_clear()
            except Exception:
                pass
            c._sel = None
            st["moved"] = False
            try:
                st["anchor"] = hit(e)
                c.focus_set()                    # so <Control-c> reaches us, as the chat Text does
            except Exception:
                st["anchor"] = None
            return "break"

        def on_motion(e):
            if st["anchor"] is None or c._item is None:
                return "break"
            try:
                cur = hit(e)
            except Exception:
                return "break"
            if cur == st["anchor"] and not st["moved"]:
                return "break"           # a still press is not a drag; don't flash a 1-char select
            st["moved"] = True
            select_chars(st["anchor"], cur)
            return "break"

        def on_release(_e):
            if not st["moved"]:          # press+release with no drag → the whole-message shortcut
                on_click()
            st["anchor"] = None
            return "break"

        def on_click(_e=None):
            """Whole-message copy. Copies `raw`, captured BEFORE _clip_bubble, because the echo
            you see is lossy on purpose: truncated at 2000 chars and with spaces injected into
            long unbroken runs to keep canvas wrapping linear."""
            copy(raw)
            return "break"

        def on_ctrl_c(_e=None):
            """Copy the highlighted part, or the whole message when nothing is highlighted.
            A partial selection copies from the DRAWN text — that is what was highlighted."""
            copy(sel_text() or raw)
            return "break"

        render()
        c.bind("<Enter>", lambda e: (st.update(hover=True), paint()))
        c.bind("<Leave>", lambda e: (st.update(hover=False), paint()))
        c.bind("<ButtonPress-1>", on_press)
        c.bind("<B1-Motion>", on_motion)
        c.bind("<ButtonRelease-1>", on_release)
        c.bind("<Control-c>", on_ctrl_c)
        c.bind("<Control-C>", on_ctrl_c)
        # Exposed for the same reason as _copy_btn's: Tk drops a synthesised <Button-1> on a
        # withdrawn widget and the suite runs withdrawn, so a test must call the handlers itself.
        c._on_click, c._on_ctrl_c = on_click, on_ctrl_c
        c._select_chars, c._sel_text = select_chars, sel_text
        c._on_press, c._on_motion, c._on_release = on_press, on_motion, on_release
        c.bind("<MouseWheel>", self._fwd_wheel)   # embedded widget must not swallow the scroll
        self._register_zoomable(c, render)
        return c

    def _bubble_sel_clear(self, keep=None):
        """Drop the highlight on whichever user bubble currently owns one. Canvas selection is
        per-canvas and every bubble is its own canvas, so starting a selection in one leaves the
        previous one lit unless something clears it by hand — this is that something."""
        prev = self._sel_bubble
        if prev is not None and prev is not keep:
            try:
                prev.select_clear()
                prev._sel = None
            except Exception:
                pass                      # bubble already destroyed by _prune_chat
        self._sel_bubble = keep

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
        # Auto-scroll-follow: driven by self._follow (see _sync_follow), NOT by re-measuring the
        # view on every delta. Measuring was fragile — a throttled giant line, an embedded table
        # or a resize leaves the view a hair off the end, which read as "user scrolled away" and
        # silently stranded the rest of the reply below the fold. see() is O(line length) on a
        # pathological newline-free GIANT line, so for such a line we still throttle the scroll to
        # ~25/s (a long stream of one huge line would otherwise monopolise the UI thread → the
        # v1.1.9-class freeze); _md_autoscroll_final catches up at turn end.
        giant = len(self._md_tail) > self.MD_LIVE_REPARSE_MAX
        scroll = (time.monotonic() - self._md_last_scroll) >= 0.04 if giant else True
        if scroll and giant:
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
        if scroll:
            self._scroll_follow()
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
        out, leaving the view a hair off the bottom). Honours the follow flag, so a user who
        scrolled up to read earlier content is left alone."""
        self._scroll_follow()
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
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._tool_chip(name, self._summ(inp, 46)),
                                padx=self.px(16), pady=self.px(3))
        self.chat.insert("end", "\n")
        self._scroll_follow()
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
        # Exposed so a test can exercise the real handler. Tk drops a synthesised
        # <Button-1> on a withdrawn widget, and the whole suite runs withdrawn on
        # purpose (no flash, no focus steal) -- so a test that "clicks" this button
        # runs nothing and then asserts against whatever is on the machine's clipboard.
        c._on_click = on_click
        c.bind("<MouseWheel>", self._fwd_wheel)   # embedded widget must not swallow the scroll
        self._register_zoomable(c, render)
        return c

    def _add_copy(self, text):
        """Drop a Copy button on its own line, left-aligned under the message it belongs to.
        No-ops on empty/whitespace text (e.g. a turn that produced only tool calls)."""
        if not (text and str(text).strip()):
            return
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._copy_btn(text), padx=self.px(16), pady=self.px(1))
        self.chat.insert("end", "\n")
        self._scroll_follow()
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
    def _maybe_offer_resume(self):
        """On launch, if the previous run left a conversation behind (its session id is
        persisted on every completed turn — see _persist_session), drop a one-click
        Resume button into the chat. Only for a session from the SAME working dir (the
        CLI stores sessions per directory) and not too old; Clear wipes the record, so
        a deliberately discarded conversation is never offered back."""
        if not RESUME_OFFER:
            return
        saved = _load_state().get("last_session")
        if not (isinstance(saved, dict) and saved.get("id")):
            return
        if saved.get("cwd") != WORKING_DIR:
            return
        ts = saved.get("ts")
        age = (time.time() - ts) if isinstance(ts, (int, float)) else -1
        if not (0 <= age <= RESUME_OFFER_MAX_AGE):
            return
        self.add_sys(f"💬 You have a conversation from {self._age_str(age)} ago. "
                     "Claude can pick it up where you left off:")
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._resume_btn_widget(str(saved["id"])),
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")
        self._scroll_follow()

    @staticmethod
    def _age_str(secs):
        """Coarse '5 min' / '3 h' / '2 d' for the resume offer — false precision would
        just be noise."""
        secs = max(0, int(secs))
        if secs < 3600:
            return f"{max(1, secs // 60)} min"
        if secs < 86400:
            return f"{secs // 3600} h"
        return f"{secs // 86400} d"

    def _resume_btn_widget(self, session_id):
        """One-click 'Resume last conversation' button embedded in the chat (same
        embedded-canvas pattern as the CLI-update button, incl. the forwarded wheel).
        Click asks the worker to relaunch the client with --resume; the outcome comes
        back as a ('resumed') / ('resume_failed') event that restyles this exact
        button. Once a NEW conversation starts (first send), the button goes stale —
        clicking it then would silently discard the messages just exchanged."""
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2",
                      takefocus=0)
        c._ustate = "idle"                          # idle | working | done | failed | stale
        st = {"f": None, "w": 0, "h": 0, "rad": 0}  # current-zoom font + box, set by render()
        labels = {"idle": "↺  Resume last conversation",
                  "working": "Resuming…",
                  "done": "✓  Resumed — keep going",
                  "failed": "⚠  Couldn't resume — this is a fresh session",
                  "stale": "↺  (a new conversation has started)"}

        def draw(hover=False):
            c.delete("all")
            s = c._ustate
            if s == "idle":
                bg = T["accent_hi"] if hover else T["accent"]
                fg = T["on_accent"]
            elif s == "failed":
                bg, fg = T["tool_bg"], T["err"]
            else:                                   # working / done / stale → inert grey
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
                c.configure(cursor="hand2" if s == "idle" else "arrow")
                draw()
            except Exception:
                pass
        c._set_ustate = set_state    # let the resumed/resume_failed handlers restyle it

        def on_click(_e):
            if c._ustate != "idle" or self.busy:
                return "break"
            set_state("working")
            self._set_status("resuming last conversation…")
            self.worker.resume(session_id)
            return "break"
        c._click = on_click          # a named handle so the routing is directly testable

        render()
        c.bind("<Enter>", lambda e: draw(hover=True))
        c.bind("<Leave>", lambda e: draw(hover=False))
        c.bind("<Button-1>", on_click)
        c.bind("<MouseWheel>", self._fwd_wheel)      # embedded widget must not swallow scroll
        self._register_zoomable(c, render)
        self._resume_btn = c
        return c

    # ── past conversations ────────────────────────────────────────────────────
    # Rendered as cards INSIDE the transcript, never in a window of their own. This product
    # exists to stop you managing windows; a history browser you have to Alt+Tab to would be
    # the exact thing it is supposed to remove. Same embedded-canvas pattern as the resume
    # button and tool chips, so the list scrolls, zooms and gets disposed of by Clear.
    SHORT_SESSION = 3          # fewer typed messages than this → folded away by default

    def show_sessions(self):
        """Scan this project's transcripts on a thread and post the rows back through ui_q.
        Off-thread because a cold scan reads megabytes of JSON (a warm one is stat()-only,
        but the first open of the day is not), and janking the UI is not acceptable in a
        window that sits on top of whatever you were doing."""
        if getattr(self, "_sessions_loading", False):
            return
        self._sessions_loading = True
        self.add_sys("\U0001f5c2  Looking through your past conversations…")

        def work():
            try:
                store = sessions.Store(WORKING_DIR,
                                       cache_dir=STATE_FILE.parent / "session-cache")
                self.ui_q.put(("sessions", (store, store.list())))
            except Exception as ex:
                self.ui_q.put(("sessions_failed", str(ex)))
        threading.Thread(target=work, name="session-scan", daemon=True).start()

    def _show_session_rows(self, store, rows):
        self._sessions_loading = False
        rows = [s for s in rows if s.id != self._session_id]     # never offer the live one
        if not rows:
            self.add_sys("No earlier conversations in this folder yet.")
            return
        long_rows = [s for s in rows if s.messages >= self.SHORT_SESSION]
        short_rows = [s for s in rows if s.messages < self.SHORT_SESSION]
        for s in long_rows:
            self._add_session_card(store, s)
        if short_rows:
            self._add_more_sessions(store, short_rows)
        self._scroll_follow()

    def _add_session_card(self, store, session):
        self.chat.window_create("end", window=self._session_card(store, session),
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")

    def _add_more_sessions(self, store, short_rows):
        """One row standing in for the throwaway conversations. Folded rather than hidden:
        'short' is a guess about importance, and a guess should be reversible."""
        mark = self.chat.index("end-1c")
        lbl = self._chip_canvas(
            f"… {len(short_rows)} shorter conversation{'s' if len(short_rows) != 1 else ''}")

        def expand(_e=None):
            try:
                self.chat.delete(mark, f"{mark} lineend +1c")
            except Exception:
                pass
            for s in short_rows:
                self._add_session_card(store, s)
            self._scroll_follow()
            return "break"
        lbl.bind("<Button-1>", expand)
        lbl._on_click = expand
        self.chat.window_create("end", window=lbl, padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")

    def _chip_canvas(self, text):
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._label = text        # drawn, not Text content, so a test cannot read it back
        st = {}

        def render():
            f = tkfont.Font(root=self.root, font=self.f_small)
            c._overlay_fonts = [f]
            st["w"], st["h"] = f.measure(text) + self.px(22), self.px(22)
            c.configure(width=st["w"], height=st["h"])
            draw(False)

        def draw(hover):
            c.delete("all")
            round_rect(c, 1, 1, st["w"] - 1, st["h"] - 1, self.px(6),
                       fill=T["tool_bg"] if hover else T["bg"], outline="")
            c.create_text(st["w"] / 2, st["h"] / 2, text=text, anchor="center",
                          font=c._overlay_fonts[0], fill=T["muted"] if hover else T["faint"])
        render()
        c.bind("<Enter>", lambda e: draw(True))
        c.bind("<Leave>", lambda e: draw(False))
        c.bind("<MouseWheel>", self._fwd_wheel)
        self._register_zoomable(c, render)
        return c

    def _session_thumb(self, path, box):
        """The session's first screenshot, scaled into `box`. Returns a PhotoImage or None.

        The thumbnail is the point of these cards: you recognise a conversation by what was
        on your screen at the time far faster than by any title, and this is the only Claude
        client that has that to show you.
        """
        if not path:
            return None
        try:
            with Image.open(path) as im:
                im.load()
                im = im.convert("RGB")
                im.thumbnail(box, Image.LANCZOS)
                return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def _session_card(self, store, session):
        """One conversation: thumbnail, title, subtitle, age + message count, and a ✕.

        Click resumes it. ✕ arms a confirm on the card itself rather than popping a dialog —
        a modal would steal focus from whatever you are actually working in, which is the
        one thing this overlay must never do.
        """
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._state = "idle"          # idle | confirm | gone | resuming
        st = {"w": 0, "h": 0}

        def render():
            c.delete("all")
            f_t = tkfont.Font(root=self.root, font=self.f_small)
            f_s = tkfont.Font(root=self.root, font=self.f_small)
            c._overlay_fonts = [f_t, f_s]
            full = max(self.px(220), self.chat.winfo_width() - 2 * self.px(34))
            pad, th, tw = self.px(9), self.px(38), self.px(60)
            st["w"], st["h"] = full, th + 2 * pad
            c.configure(width=full, height=st["h"])
            hover = getattr(c, "_hover", False) and c._state == "idle"
            round_rect(c, 1, 1, full - 1, st["h"] - 1, self.px(9),
                       fill=T["tool_bg"] if hover else T["field"], outline="")

            if c._state == "gone":
                c.create_text(full / 2, st["h"] / 2, text="✓  Deleted", anchor="center",
                              font=f_t, fill=T["faint"])
                return
            if c._state == "confirm":
                c.create_text(pad + self.px(4), st["h"] / 2, anchor="w", font=f_t,
                              fill=T["text"], text="Delete this conversation?")
                c.create_text(full - pad - self.px(4), st["h"] / 2, anchor="e", font=f_t,
                              fill=T["muted"], text="Cancel", tags="no")
                c.create_text(full - pad - f_t.measure("Cancel") - self.px(18), st["h"] / 2,
                              anchor="e", font=f_t, fill=T["err"], text="Delete", tags="yes")
                return

            x = pad
            photo = getattr(c, "_photo", None)
            if photo is not None:
                c.create_image(x, st["h"] / 2, image=photo, anchor="w")
                x += tw + self.px(10)
            right = full - pad - self.px(16)
            c.create_text(x, pad + self.px(2), text=_fit(session.title, f_t, right - x),
                          anchor="nw", font=f_t, fill=T["text"])
            meta = f"{self._age_str(session.age)} ago  ·  {session.messages} message" \
                   f"{'s' if session.messages != 1 else ''}"
            sub = session.subtitle
            c.create_text(x, pad + self.px(16), anchor="nw", font=f_s, fill=T["faint"],
                          text=_fit(f"{meta}   {sub}" if sub else meta, f_s, right - x))
            c.create_text(full - pad, st["h"] / 2, text="✕", anchor="e", font=f_t,
                          fill=T["faint"], tags="del")

        def _fit(text, font, width):
            text = text or ""
            if width <= 0 or font.measure(text) <= width:
                return text
            while text and font.measure(text + "…") > width:
                text = text[:-1]
            return text + "…"

        def set_state(s):
            c._state = s
            c.configure(cursor="hand2" if s in ("idle", "confirm") else "arrow")
            render()

        def on_click(_e=None):
            if c._state != "idle" or self.busy:
                return "break"
            set_state("resuming")
            self._set_status("resuming that conversation…")
            self.worker.resume(session.id)
            return "break"

        def arm(_e=None):
            if c._state == "idle":
                set_state("confirm")
            return "break"

        def do_delete(_e=None):
            if c._state != "confirm":
                return "break"
            set_state("gone" if store.delete(session) else "idle")
            if c._state == "idle":
                self.add_sys("⚠ Couldn't delete that conversation — the file is in use.")
            return "break"

        c._on_click, c._arm, c._delete = on_click, arm, do_delete
        c._cancel = lambda _e=None: (set_state("idle"), "break")[1]
        c._photo = self._session_thumb(session.thumb, (self.px(60), self.px(38)))
        render()
        c.bind("<Enter>", lambda e: (setattr(c, "_hover", True), render()))
        c.bind("<Leave>", lambda e: (setattr(c, "_hover", False), render()))
        c.bind("<Button-1>", on_click)
        c.tag_bind("del", "<Button-1>", arm)
        c.tag_bind("yes", "<Button-1>", do_delete)
        c.tag_bind("no", "<Button-1>", c._cancel)
        c.bind("<MouseWheel>", self._fwd_wheel)
        self._register_zoomable(c, render)
        return c

    def _persist_session(self):
        """Record the current conversation's session id (+ when and where) so the next
        launch can offer to resume it. Called per completed turn — cheap (a tiny JSON
        write) and crash-safe: whatever the last finished turn was, that's resumable."""
        if self._session_id:
            _save_state(last_session={"id": self._session_id, "ts": time.time(),
                                      "cwd": WORKING_DIR})

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
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._cli_update_btn(latest),
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")
        self._scroll_follow()
        self._prune_chat()

    def _one_click_update_btn(self, labels, work, thread_name, btn_attr, result_kind):
        """Shared builder for the in-chat one-click update buttons — the CLI's and the overlay's
        own (same embedded-canvas pattern as the Copy button). `labels` gives the text for the four
        states; a click while idle/error runs `work()` (zero-arg, returns (ok, msg)) on a
        background thread named `thread_name`, records THIS canvas on `self.<btn_attr>` so the
        result handler can restyle the button the user actually clicked, and delivers the outcome
        as a (`result_kind`, (ok, msg)) UI event. A click while 'done' restarts the overlay —
        neither update takes effect in this process. Forwards the wheel so it can't swallow
        scrolling (the v1.4.1 embedded-widget trap).

        One builder rather than two: what differs between the CLI button and the overlay button is
        exactly the five arguments above. The drawing, the zoom re-render, the hover states and
        the click routing were identical, and a second copy of them is a second place where a
        state bug has to be found and fixed."""
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._ustate = "idle"                              # idle | working | done | error
        st = {"f": None, "w": 0, "h": 0, "rad": 0}      # current-zoom font + box, set by render()

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
                setattr(self, btn_attr, c)
                def run():
                    try:
                        ok, msg = work()
                    except Exception as e:
                        ok, msg = False, type(e).__name__
                    self.ui_q.put((result_kind, (bool(ok), str(msg))))
                threading.Thread(target=run, name=thread_name, daemon=True).start()
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

    def _cli_update_btn(self, latest):
        """The 'your CLI is behind' button: runs `npm install -g @anthropic-ai/claude-code@latest`
        (cliupdate.run_update) off the UI thread, then offers a restart so the newest models load.
        Imported inside the worker so a broken/absent cliupdate can't cost us the button."""
        def work():
            from cliupdate import run_update
            return run_update()
        return self._one_click_update_btn(
            {"idle": f"⬆  Update CLI to v{latest}",
             "working": "Updating…  (≈1 min)",
             "done": "✓  Updated — click to restart",
             "error": "⚠  Update failed — click to retry"},
            work, "cli-update", "_cli_update_btn_ref", "cli_update_result")

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

    # ── "a newer overlay exists" notice + one-click update (runs update.cmd) ────────────

    def _show_overlay_update_notice(self, latest):
        """Render the 'a newer overlay is out' notice. On a git clone we can update in place, so
        the notice carries a one-click button that runs update.cmd; a ZIP install has nothing to
        pull, so it gets the manual instructions instead of a button that can only refuse.
        Shown at most once per session — the check runs once at startup, but a re-render should
        not stack a second button whose result would restyle only the newest one."""
        if getattr(self, "_ov_update_shown", False):
            return
        self._ov_update_shown = True
        head = f"🔔 Update available: v{latest} (you have v{__version__}). "
        if not can_self_update():
            self.add_sys(head + "Close the overlay and run update.cmd (or: git pull) to upgrade.")
            return
        self.add_sys(head + "Update in one click — a console window opens and shows the pull, "
                            "the packages and the check that the new code still starts. You "
                            "don't have to close the overlay first:")
        self.chat.insert("end", "\n")
        self.chat.window_create("end", window=self._ov_update_btn(latest),
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")
        self._scroll_follow()
        self._prune_chat()

    def _ov_update_btn(self, latest):
        """The 'update the overlay itself' button. Runs update.cmd in its own console and waits
        for it (win32utils.run_overlay_update), then restarts — this process keeps running the
        modules it imported at launch, so nothing pulled takes effect until it does. The 'done'
        label reports that restart rather than asking for it; the click is still wired to
        _restart_overlay so the button remains the way out if the automatic one can't start."""
        def work():
            return run_overlay_update()      # looked up at click time, so it stays patchable
        return self._one_click_update_btn(
            {"idle": f"⬆  Update overlay to v{latest}",
             "working": "Updating…  (in the console window)",
             "done": "✓  Updated — restarting…",
             "error": "⚠  Update failed — click to retry"},
            work, "overlay-update", "_ov_update_btn_ref", "ov_update_result")

    def _show_overlay_update_result(self, payload):
        """Restyle the Update-overlay button and print the follow-up: success → say the restart
        is happening and do it; failure → the reason plus the manual route, since the console
        that explained it may already be closed.

        The restart is automatic because a successful update leaves the app in a state nobody
        wants to be left in: the new code is on disk, this window is still the old one, and the
        only thing standing between them is a click that carries no decision — declining it just
        means running code you already replaced. _restart_overlay hands off to a detached fresh
        instance and only quits this one once that started, so a relaunch that fails leaves the
        window and an error rather than nothing at all."""
        try:
            ok, msg = payload
        except Exception:
            ok, msg = False, str(payload)
        c = getattr(self, "_ov_update_btn_ref", None)
        if c is not None:
            try:
                c._set_ustate("done" if ok else "error")
            except Exception:
                pass
        if ok:
            self.add_sys("✅ Overlay updated. The new code is on disk, but this window is still "
                         "running the old one — restarting now. The fresh window offers to "
                         "resume this conversation.")
            self._restart_overlay()
        else:
            self.add_err(f"Overlay update didn't complete — {msg}. You can also update it by "
                         "hand: double-click update.cmd in the app folder.")

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
        # "Your next message is unaffected" is TRUE for a transient failure (overload, rate
        # limit, max turns) but flatly wrong for an authentication failure: that one repeats
        # forever, so promising otherwise sends the user off writing messages that can't be
        # delivered. Say what actually fixes it instead.
        if authstate.is_auth_error_text(detail) or authstate.is_auth_error_text(subtype):
            return (f"Last turn ended with an error ({reason}). The Claude CLI's login is no "
                    f"longer valid, so further messages will fail too — sign in again in a "
                    f"terminal:  {self._AUTH_FIX}")
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

    # ── the CLI's login ──
    _AUTH_FIX = "claude auth login"      # the one command that repairs it, in a terminal

    def _auth_notice(self):
        """The full explanation + fix, shown once per death (see _auth_watchdog)."""
        return ("⚠ The Claude CLI's login has expired and it cleared its stored credentials, "
                "so every message will fail until you sign in again — restarting the overlay "
                f"cannot fix this.\n    In a terminal, run:  {self._AUTH_FIX}\n    "
                "(if it complains, run  claude auth logout  first). The overlay notices the "
                "new login on its own — no restart needed.")

    def _auth_watchdog(self, now):
        """Poll the CLI's stored login and announce each TRANSITION, so a dead login is
        reported the moment it happens (or the moment the overlay is opened onto one) rather
        than only when the user sends and loses a message. Cheap: one stat(), plus a ~1KB
        JSON read only when the file changed. Recovery is announced too — after signing in
        elsewhere, the user shouldn't have to guess whether the overlay noticed."""
        if now - self._auth_checked < AUTH_CHECK_INTERVAL:
            return
        self._auth_checked = now
        try:
            dead = authstate.dead_reason() is not None
        except Exception:
            return                       # never let a credential read break the pump
        if dead == self._auth_dead:
            return
        self._auth_dead = dead
        if dead:
            self._auth_told = True
            dbg("auth", "stored login is blank — sends held back")
            self.add_err(self._auth_notice())
        else:
            self._auth_told = False
            dbg("auth", "stored login looks usable again")
            self.add_sys("✅ Claude CLI login restored — your next message will go through.")

    def _auth_blocks_send(self):
        """True when this send must be held back because the CLI's stored login is provably
        unusable. Nothing is consumed when it returns True, so the typed text and every
        attachment stay exactly where they are — that is the whole point: the failing turn
        would otherwise swallow both. Re-checked here rather than trusting the watchdog's
        cached flag, so a death that happened seconds ago is caught on this very send."""
        if not AUTH_GATE:
            return False
        # Nothing to send anyway (empty box, no auto-shot, no attachments): stay quiet and let
        # the normal empty-send no-op happen, so Enter on an empty box can't nag about login.
        if not (self._entry_text() or self.auto_shot or self.pending_shot or self.pending_images):
            return False
        try:
            if authstate.dead_reason() is None:
                return False
        except Exception:
            return False                 # no evidence → never stand in the way of a send
        self._auth_dead = True
        self._auth_checked = time.monotonic()      # the watchdog needn't re-announce this
        if not self._auth_told:
            self._auth_told = True
            self.add_err(self._auth_notice())
        else:
            self.add_err("⚠ Still signed out — nothing was sent. Run "
                         f"{self._AUTH_FIX} in a terminal; your message is still here.")
        return True

    def _send_or_stop(self):
        if self.busy:
            self.worker.interrupt()
            self._set_status("stopping…")
            return
        if self._auth_blocks_send():     # BEFORE anything is consumed (text, shot, attachments)
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
        self._last_sent = (text, images)   # a turn refused for allowance never reached Claude;
                                           # _restore_draft hands the text back (see "result")
        self._cancel_retry()               # sending by hand IS the retry — whether this is the
                                           # armed message or a different one, the schedule has
                                           # been overtaken and must not fire later on its own
        # Auto-screenshots only: drop any capture that the model already has — the same bytes,
        # or (far more often, since a live desktop never re-encodes identically) the same
        # picture. Re-attaching buys nothing and costs real latency (measured 2026-08:
        # +0.7-2.6s TTFT per message for one monitor's inline image) plus vision tokens that
        # stay spent for the rest of the conversation. A manual Snap is an explicit "attach
        # it" and is never deduped. Legacy "read" mode isn't either: its old file may already
        # be pruned from disk, so "refer to the previous one" can dangle.
        unchanged = []
        if self.auto_shot and shots and IMAGE_INPUT == "inline":
            shots, unchanged = self._dedupe_shots(shots)
        n = (len(shots) if shots else 0) + len(images)
        label = text if text else "(look at my screens)"
        if n:
            label += (f"   🖼×{n}" if n > 1 else "   🖼")
        elif unchanged:
            label += "   🖼 unchanged"
        self.add_user(label)
        if self._resume_btn is not None:   # a NEW conversation is starting — resuming now
            try:                           # would silently discard it; retire the offer
                self._resume_btn._set_ustate("stale")
            except Exception:
                pass
            self._resume_btn = None
        if IMAGE_INPUT == "inline":
            paths = [s["path"] for s in (shots or [])] + list(images)
            self.worker.ask(self._inline_text(text, shots, images, unchanged), paths)
        else:
            self.worker.ask(self._build_prompt(text, shots, images), [])
        self._set_busy(True)

    @staticmethod
    def _shot_key(s):
        """What a screenshot is OF: dedupe must never compare across targets (window-scope
        vs a monitor, or monitor 0 vs 1) — identical bytes for different targets is
        practically impossible anyway, but the keying keeps the intent explicit."""
        return ("window",) if s.get("window") is not None else ("mon", s.get("index"))

    def _dedupe_shots(self, shots):
        """Split auto-captured shots into (changed, unchanged) against the last fingerprint
        the model verifiably has, and stage the new ones as PENDING — they're promoted only
        when the turn returns a clean result (see _handle "result"/"turn_done"), because a
        turn that errors out or is stopped may never have delivered the image. Any read
        failure counts as changed — when in doubt, attach (a stale "unchanged" pointer is
        worse than 1s extra).

        Two tests, cheapest first: identical bytes, then _shot_phash for the far more common
        case of a screen that hasn't meaningfully changed but re-encodes differently anyway.
        Comparison is always against the BASELINE the model holds, not against the previous
        capture, so a screen drifting a bit per turn still re-attaches once the accumulated
        drift matters — which is the honest reading of "has this changed since you saw it"."""
        keep, unchanged = [], []
        for s in shots:
            try:
                digest = hashlib.sha256(Path(s["path"]).read_bytes()).hexdigest()
            except Exception:
                keep.append(s)
                continue
            key = self._shot_key(s)
            prev = self._sent_shot_hashes.get(key)
            prev_sha, prev_look = prev if isinstance(prev, tuple) else (prev, None)
            if prev_sha == digest:
                unchanged.append(s)
                continue
            look = _shot_phash(s["path"])     # only worth decoding once the bytes differ
            if _shot_looks_same(prev_look, look):
                unchanged.append(s)
                continue
            keep.append(s)
            self._pending_shot_hashes[key] = (digest, look)
        return keep, unchanged

    def _forget_sent_shots(self):
        """The conversation context can no longer be trusted to contain the previously
        sent screenshot(s) — attach fresh next time."""
        self._sent_shot_hashes.clear()
        self._pending_shot_hashes.clear()

    def _inline_text(self, text, shots, images, unchanged=None):
        """Short text companion for inline-image turns: the model sees the images
        directly, so we only add a one-line note about what's attached. Deduped
        captures (see _dedupe_shots) become an explicit "unchanged" pointer instead —
        the model must know it can trust the previous screenshot, or it may assume it
        has no current view of the screen at all."""
        note = []
        if unchanged:
            tags = ", ".join(
                (f"the “{s['window']}” window" if s.get("window") is not None
                 else f"monitor {s['index']}") for s in unchanged)
            note.append(f"[My screen ({tags}) is UNCHANGED since the most recent screenshot "
                        f"of it earlier in this conversation — keep using that one. If you "
                        f"no longer have that screenshot in context, say so instead of "
                        f"guessing.]")
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
        if text:
            body = text
        elif shots or images:
            body = ("Look at the attached screen(s)/image(s) and tell me "
                    "what's there / what I might want help with.")
        else:   # everything was deduped away — point at the context copy instead
            body = ("Look at my screen (the most recent screenshot earlier in this "
                    "conversation — it hasn't changed) and tell me what I might want "
                    "help with.")
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
        self._ctx_tokens = None
        self._ctx_hist.clear()          # a new conversation burns at its own rate, not the old
        self._ctx_warned = 0.0          # …and has earned the warning back
        self._ctx_sample_due = False
        self._last_sent = None          # a thrown-away conversation's draft must not come back
                                        # (the quota reading itself survives: the allowance is
                                        # the account's, not this conversation's)
        self._cancel_retry()            # …and its scheduled retry must not fire into the new one
        self._refresh_statusline()
        # Clear = deliberate discard: forget the session AND its persisted record, so
        # the next launch can't offer to resume a conversation the user threw away.
        self._session_id = None
        self._resume_btn = None          # the embedded button was just wiped with the chat
        # Guard the window until the worker confirms the reset (reset_done): a stale
        # (session / turn_done) batch from the turn that was in flight when Clear was
        # clicked would otherwise re-set _session_id and re-persist the discarded record.
        self._discard_pending = True
        # Forget dedupe state NOW, at click time — not only when reset_done confirms. A
        # send slipped in between (the worker is busy reconnecting, but the UI isn't busy)
        # would otherwise dedupe against the discarded conversation and land an
        # image-less "unchanged" prompt in the brand-new session.
        self._forget_sent_shots()
        _save_state(last_session=None)
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
        # Forget dedupe state at click time, same reasoning as reset(): a send queued
        # behind the compaction would otherwise dedupe against images the imminent
        # summary may drop. (compact_done clears again — that one also covers failures.)
        self._forget_sent_shots()
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
        # version goes last so it clips first if the window is narrow; ⬆ flags an update
        ver = f"v{__version__}" + ("  ⬆" if self._update_available else "")
        self.statusline.configure(text=f"{self._model or 'Claude'} ▾", fg=T["muted"])
        self.ctx_lbl.configure(text=f"·   {self._gauge_text()}", fg=self._ctx_color())
        self.ver_lbl.configure(text=f"·   {ver}", fg=T["muted"])
        self._paint_quota_ring()

    # ── the middle of the statusline ──
    def _ctx_text(self):
        """Context as a percentage, and nothing else.

        The headroom in turns used to be appended here, which meant the row grew a second
        clause the moment a burn rate could be read and lost it again after a compaction. The
        one strip of chrome that should hold still was reflowing while you looked at it. The
        figure is not gone - _usage_panel_text carries it, one hover away, and the end-of-turn
        warning still speaks it."""
        p = f"{self._ctx_pct:.0f}%" if isinstance(self._ctx_pct, (int, float)) else "—"
        return f"context {p}"

    def _gauge_text(self):
        """What the row carries when the mark is not being hovered: context.

        The allowance used to own this slot, with context demoted to a fallback and allowed
        back only once it was over its own warning line - two percentages competing for one
        place. The allowance lives on the ring now, and printing it here as well would be the
        same number twice; on a narrow overlay that duplication is what pushed the version off
        the end. Context is NOT duplicated by the ring: the ring is the plan allowance, context
        is the size of THIS conversation, and the two answer different questions. So context
        simply keeps the slot, and the competition this method used to arbitrate is gone.
        """
        return self._ctx_text()

    def _usage_panel_text(self):
        """Everything about usage, in one aligned block, for the panel the mark opens.

        No unlabelled gauge explains itself. What makes one learnable is being able to
        interrogate it, and the answer belongs where the asking happened - beside the mark,
        not down in a status row that then reflows under the cursor. Both allowance windows
        and context sit here together because they are the same question asked three ways, and
        because context's turns figure had to leave the row to stop it moving."""
        rows = []
        wins = self._ring_windows()
        for key, label in (("five_hour", "5h"), ("week", "week")):
            u = (wins.get(key) or {}).get("utilization")
            if isinstance(u, bool) or not isinstance(u, (int, float)):
                continue
            rows.append((label, f"{u * 100:.0f}%", self._quota_resets_text(wins[key])))
        if not rows:
            rows.append(("allowance", "—", "no reading yet"))
        p = self._ctx_pct
        if isinstance(p, (int, float)):
            left = self._ctx_turns_left()
            rows.append(("context", f"{p:.0f}%",
                         f"~{left} turn{'' if left == 1 else 's'} left" if left is not None else ""))
        w = max(len(r[0]) for r in rows)
        return "\n".join(f"{a.ljust(w)}   {b:>4}   {c}".rstrip() for a, b, c in rows)

    def _mark_enter(self, _e=None):
        self._usage_panel.configure(text=self._usage_panel_text())
        # Just under the titlebar, left-aligned with the mark it belongs to.
        self._usage_panel.place(x=self.px(10), y=self.px(42))
        self._usage_panel.lift()

    def _mark_leave(self, _e=None):
        self._usage_panel.place_forget()

    def _maybe_explain_ring(self):
        """Name the ring once, the first time there is something to point at.

        A first-time reader has no way to guess that two arcs around a logo are a plan
        allowance — the shape can carry the numbers but not their meaning. One sentence, said
        once, is the only thing that closes that gap; after that the hover carries it."""
        if self._ring_explained or not self._ring_arcs():
            return
        self._ring_explained = True
        self.add_sys("◔ The ring on ✻ is your plan allowance — the inner arc is the 5-hour "
                     "window, the outer one is weekly. Hover the mark for the numbers.")

    def _gauge_quota(self):
        """The freshest allowance reading, for DISPLAY only.

        usage.py polls every minute; the CLI speaks only when its status transitions, so its
        copy can be hours old — and while the overlay sits idle, hours old is exactly when the
        number is worth looking at. So the poll wins the gauge whenever it has one.

        It wins nothing else. _announce_quota and _offer_retry read self._quota directly and
        deliberately: the polled reading carries no status (usage.py won't invent one from the
        endpoint's severity vocabulary), and a warning that speaks, or a retry that puts a
        message on the wire hours later, must come from the CLI's own event or not at all."""
        w = (self._quota_polled or {}).get("windows")
        if isinstance(w, dict) and w:
            return _binding_window(w) or {}
        return self._quota or {}

    def _quota_windows(self):
        """Every allowance window we can place on the ring, keyed by name.

        The poll carries all of them. The CLI's own event carries exactly one, and only
        sometimes names it — an unnamed one is DROPPED rather than parked on whichever track
        is handy, because putting a weekly reading on the 5-hour arc would be a lie the user
        has no way to see through. The statusline text still prints that number, so staying
        silent here costs nothing.
        """
        w = (self._quota_polled or {}).get("windows")
        if isinstance(w, dict) and w:
            return w
        q = self._quota or {}
        u, name = q.get("utilization"), q.get("window")
        if name in _QUOTA_WINDOWS and isinstance(u, (int, float)) and not isinstance(u, bool):
            return {name: {"utilization": float(u), "resets_at": q.get("resets_at")}}
        return {}

    def _ring_color(self, u, quiet):
        """Recessive until it isn't. An arc carries no number, so colour is the only channel
        it has for urgency — hence its own amber step rather than waiting for the CLI's."""
        if u >= _QUOTA_HOT:
            return T["err"]
        if u >= _QUOTA_WARN:
            return T["accent"]
        return quiet

    def _ring_track(self):
        """The empty part of a gauge, in a tone you can actually see.

        The first cut used T["border"], which is 1.2:1 against the surface — exactly right for
        a hairline between two panels and completely invisible as a track, so a fresh overlay
        with no reading yet drew a mark that looked untouched. _contrast pins the replacement
        rather than trusting the eye that missed it the first time."""
        return _mix(T["bg"], T["faint"], 0.70)   # 1.8:1 light, 2.1:1 dark — seen, not shouted

    def _ring_windows(self):
        """The two tracks, each resolved to the one window it stands for. Shared by the ring
        and by the hover text so a number and its label can never come from different windows."""
        wins = self._quota_windows()
        return {"five_hour": wins.get("five_hour"),
                "week": _binding_window({k: v for k, v in wins.items() if k != "five_hour"})}

    def _ring_arcs(self):
        """What each track should show: {track: (fraction, colour)}, tracks with nothing to
        say left out. Kept separate from the drawing so the numbers and colours can be tested
        without decoding a bitmap."""
        out = {}
        for key, w in self._ring_windows().items():
            u = (w or {}).get("utilization")
            if isinstance(u, bool) or not isinstance(u, (int, float)) or u <= 0.005:
                continue           # a hairline at 0% would read as "something is used"
            out[key] = (min(1.0, float(u)), self._ring_color(u, T["muted"]))
        return out

    def _paint_quota_ring(self):
        """The ✻ mark, ringed by two allowance gauges: the 5-hour window inside, the weekly
        one outside.

        WHY TWO, AND WHY HERE. The status row had one slot and filled it with whichever window
        was furthest along, labelled "(5h)" or "(week)". That works as text because the text
        says which window it is describing — but the row was also carrying the model name, the
        reset time and the version, and on a narrow overlay the version was being clipped.
        Moving the gauge onto the mark buys the row back; the catch is that an arc cannot carry
        the label, so a single arc that silently changed meaning would be strictly worse than
        the text it replaced. Two fixed tracks answer both at once: position IS the label, and
        the thicker inner one is the window that ends the session you are sitting in.

        Two arcs are also the honest reading of the data. The weekly window climbs slowly in
        the background; the 5-hour one can go from a tenth to spent in an afternoon. Ranking
        them by magnitude hid the 5-hour window for exactly as long as it sat below the weekly
        number — which is where it is every time you sit down to work. Both are the same unit
        (share of an allowance) on the same 0-1 scale, so drawing them together is one scale,
        not two. The sweep runs clockwise from 12 o'clock, the direction a clock face reads,
        which is what a window that empties and refills on a timer actually is.

        WHY AN IMAGE. Tk's create_arc is not antialiased on Windows, and a 3px stroke on a 36px
        circle comes out a visible staircase — the first cut of this was drawn with canvas
        primitives and was unreadable at real size. Everything is rendered at _RING_SS× into
        one PIL image, downsampled, and placed as a single canvas item, so every curve
        (including the ✻ itself) is smooth. The photo is kept on the instance because Tk holds
        only a weak claim on a PhotoImage — drop the Python reference and the mark goes blank.
        """
        c = getattr(self, "_mark", None)
        if c is None:              # a repaint can land before the titlebar is built
            return
        sz = self._mark_sz
        S = sz * _RING_SS
        im = Image.new("RGB", (S, S), T["bg"])
        d = ImageDraw.Draw(im)
        arcs, track = self._ring_arcs(), self._ring_track()
        for key, rf, wf in _RING_GEOM:
            r, lw = S * rf, max(1, round(S * wf))
            box = [S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r]
            # The empty track is always drawn: an arc with nothing behind it reads as a
            # fragment of something rather than as "this much of that".
            d.arc(box, 0, 360, fill=track, width=lw)
            a = arcs.get(key)
            if a:
                d.arc(box, -90, -90 + 360 * a[0], fill=a[1], width=lw)
        self._draw_spark_pil(d, S)
        self._ring_photo = ImageTk.PhotoImage(im.resize((sz, sz), Image.LANCZOS))
        c.delete("ring")
        c.create_image(sz / 2, sz / 2, image=self._ring_photo, tags="ring")

    def _draw_spark_pil(self, d, S):
        """The ✻ at the centre of the mark, into the same supersampled image as the rings.
        Round tips are ellipses because PIL has no cap style; at _RING_SS× they land as the
        same shape Tk's capstyle="round" used to give."""
        import math
        cx = cy = S / 2
        r, w = S * _SPARK_R, max(1, round(S * 2 / 32))
        for i in range(12):
            a = math.pi * i / 6
            r1 = r if i % 2 == 0 else r * 0.5
            x, y = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
            d.line([cx, cy, x, y], fill=T["accent"], width=w)
            d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=T["accent"])

    def _quota_resets_text(self, q):
        """When the allowance comes back, as a wall clock. A live countdown would need a timer
        redrawing a number nobody watches tick; the time you can start again is the thing you
        actually plan around. Weekly windows can reset days out, so those name the day too.

        Takes the reading to describe, because its two callers want different ones: the gauge
        shows whichever is freshest, while an announcement must quote the same event whose
        status it is announcing — mixing them could pair "weekly allowance is used up" with
        the 5-hour window's reset time."""
        ts = (q or {}).get("resets_at")
        if not isinstance(ts, (int, float)) or ts <= 0:
            return ""
        try:
            fmt = "%H:%M" if ts - time.time() < 20 * 3600 else "%a %H:%M"
            return "resets " + time.strftime(fmt, time.localtime(ts))
        except Exception:
            return ""

    def _gauge_color(self):
        # Same reading the text came from, so colour and number can never describe different
        # windows. A polled reading has no status, which is why _QUOTA_HOT colours by the
        # number: an amber tier we'd have to guess at is worse than a red one we can prove.
        q = self._gauge_quota()
        u = q.get("utilization")
        if isinstance(u, (int, float)):
            st = q.get("status")
            if st == "rejected" or u >= _QUOTA_HOT:
                return T["err"]
            if st == "allowed_warning":
                return T["accent"]
            return T["muted"]
        return self._ctx_color()

    def _ctx_color(self):
        p = self._ctx_pct
        if not isinstance(p, (int, float)):
            return T["muted"]
        if p >= _CTX_HOT_PCT:
            return T["err"]
        if p >= _CTX_WARN_PCT:
            return T["accent"]
        return T["muted"]

    def _ctx_rate(self):
        """Context percent consumed per turn over the recent window, or None when there's no
        usable slope — fewer than two turns recorded, or a window that only went down."""
        h = self._ctx_hist
        if len(h) < 2:
            return None
        rate = (h[-1] - h[0]) / (len(h) - 1)
        return rate if rate > 0 else None

    def _ctx_turns_left(self):
        """Whole turns of headroom at the current burn rate, or None if it can't be known.

        Measured against a full window rather than the point where the CLI decides to
        auto-compact, which we don't get told: the number is prefixed "~" and paired with a
        warning that fires well before either, so it's a budget, not a countdown to a cliff."""
        rate = self._ctx_rate()
        if rate is None or not isinstance(self._ctx_pct, (int, float)):
            return None
        return max(0, int((100.0 - self._ctx_pct) / rate))

    def _note_ctx_turn(self):
        """Record where the context stood at the end of a turn, then warn if that's a new tier.

        One sample per TURN, not per usage event: the rate worth showing is "how many more
        messages do I get", and messages are the unit the user spends. A drop means something
        won room back (a compaction, explicit or the CLI's own), so the old slope no longer
        describes the new conversation — start the window over rather than average across it."""
        p = self._ctx_pct
        if not isinstance(p, (int, float)):
            return
        h = self._ctx_hist
        if h and p < h[-1]:
            h.clear()
            self._ctx_warned = 0.0        # room won back → the warning is worth making again
        h.append(float(p))
        del h[:-_CTX_RATE_TURNS]
        self._refresh_statusline()
        self._maybe_warn_ctx()

    def _maybe_warn_ctx(self):
        """Say something ONCE per tier, at the end of a turn — the only moment at which
        compacting is free. The CLI's own auto-compaction fires mid-answer, and running out
        entirely ends the session; both are avoidable, but only if you're told in time."""
        p = self._ctx_pct
        if not isinstance(p, (int, float)) or self._compacting:
            return
        tier = _CTX_HOT_PCT if p >= _CTX_HOT_PCT else (_CTX_WARN_PCT if p >= _CTX_WARN_PCT else 0.0)
        if not tier or tier <= self._ctx_warned:
            return
        self._ctx_warned = tier
        left = self._ctx_turns_left()
        room = f" — about {left} more turn{'' if left == 1 else 's'} at this rate" if left else ""
        self.add_sys(f"◔ Context {p:.0f}% full{room}. {self._compact_advice()}")

    def _announce_quota(self):
        """Speak once per transition. The CLI only emits on change, but a reconnect replays
        the current status, so remember what was already said rather than trust the stream.

        This is the warning the overlay never had. Being cut off mid-task is what running out
        of allowance feels like from the outside, and the entire value of having the number is
        seeing it coming while there's still something to do about it."""
        q = self._quota or {}
        st = q.get("status")
        if st == self._quota_said:
            return
        self._quota_said = st
        if st not in ("allowed_warning", "rejected"):
            return
        win = _QUOTA_WINDOWS.get(q.get("window")) or "usage"
        resets = self._quota_resets_text(q)
        when = f" — {resets}" if resets else ""
        if st == "rejected":
            self.add_err(f"◔ Your {win} allowance is used up{when}.")
        else:
            u = q.get("utilization")
            used = f"{u * 100:.0f}% of" if isinstance(u, (int, float)) else "close to"
            self.add_sys(f"◔ {used} your {win} allowance is spent{when}. A smaller model "
                         f"stretches what's left — click {self._model or 'the model'} ▾. "
                         f"Turning Auto-shot off saves the most per message.")

    def _restore_draft(self):
        """Put a refused message back in the box.

        A turn rejected for allowance never reached Claude, so the text is simply gone — and
        it's gone at the exact moment the user has to wait hours to try again, which is the
        worst possible time to have to remember what they were about to ask. Only fills an
        EMPTY box: whatever they've started typing since outranks anything we kept."""
        kept = (self._last_sent or ("", []))[0]
        if not kept or self._entry_text():
            return False
        self._ph_out()
        self.entry.insert("1.0", kept)
        self._ph_active = False
        self.entry.configure(fg=T["text"])
        return True

    # ── retrying when the allowance comes back ──
    def _offer_retry(self, text):
        """Offer to send a refused message the moment the allowance returns.

        Opt-in, and deliberately so: arming this puts a message on the wire hours later,
        quite possibly with nobody at the machine. That's a choice to make on purpose, not a
        default to discover after the fact — so the overlay offers, and the user decides.

        Needs a reset time to aim at. Without one there's nothing to schedule and the
        restored draft stands on its own, which is still the important half."""
        when = (self._quota or {}).get("resets_at")
        if not isinstance(when, (int, float)) or when <= time.time() or not text:
            return
        self._cancel_retry()                    # never stack two offers
        self._retry = {"at": int(when), "text": text, "armed": False}
        self.chat.insert("end", "\n")
        self._retry_btn = self._retry_btn_widget()
        self.chat.window_create("end", window=self._retry_btn,
                                padx=self.px(16), pady=self.px(2))
        self.chat.insert("end", "\n")
        self._scroll_follow()

    def _retry_when(self):
        r = self._retry or {}
        ts = r.get("at")
        if not isinstance(ts, (int, float)):
            return ""
        fmt = "%H:%M" if ts - time.time() < 20 * 3600 else "%a %H:%M"
        return time.strftime(fmt, time.localtime(ts))

    def _retry_btn_widget(self):
        """Arm/cancel button for the scheduled retry — same embedded-canvas pattern as the
        Copy and Update CLI buttons, including the wheel forward so it can't swallow scroll."""
        c = tk.Canvas(self.chat, bg=T["bg"], highlightthickness=0, cursor="hand2", takefocus=0)
        c._ustate = "idle"                         # idle | armed | sent | off
        st = {"f": None, "w": 0, "h": 0, "rad": 0}
        when = self._retry_when()
        labels = {"idle":  f"⏱  Send it automatically at {when}",
                  "armed": f"⏱  Waiting for {when} — click to cancel",
                  "sent":  "✓  Sent when the allowance came back",
                  "off":   "Cancelled — send it yourself whenever"}

        def draw(hover=False):
            c.delete("all")
            s = c._ustate
            if s == "idle":
                bg = T["accent_hi"] if hover else T["accent"]
                fg = T["on_accent"]
            elif s == "armed":
                bg, fg = (T["hover"] if hover else T["tool_bg"]), T["accent"]
            else:                                  # sent / off → inert
                bg, fg = T["tool_bg"], T["muted"]
            round_rect(c, 1, 1, st["w"] - 1, st["h"] - 1, st["rad"], fill=bg, outline="")
            c.create_text(st["w"] / 2, st["h"] / 2, text=labels[c._ustate], fill=fg,
                          font=st["f"], anchor="center")

        def render():
            f = tkfont.Font(root=self.root, font=self.f_small)
            c._overlay_fonts = [f]
            pad = self.px(11)
            widest = max(f.measure(v) for v in labels.values())   # widest state → no reflow
            st.update(f=f, h=self.px(24), rad=self.px(7), w=pad + widest + pad)
            c.configure(width=st["w"], height=st["h"])
            draw()

        def set_state(s):
            c._ustate = s
            try:
                c.configure(cursor="hand2" if s in ("idle", "armed") else "arrow")
                draw()
            except Exception:
                pass
        c._set_ustate = set_state

        def on_click(_e):
            if c._ustate == "idle":
                self._arm_retry()
            elif c._ustate == "armed":
                self._cancel_retry(note="⏱ Auto-send cancelled.")
            return "break"                         # sent / off → inert
        c._click = on_click

        render()
        c.bind("<Enter>", lambda e: draw(hover=True))
        c.bind("<Leave>", lambda e: draw(hover=False))
        c.bind("<Button-1>", on_click)
        c.bind("<MouseWheel>", self._fwd_wheel)
        self._register_zoomable(c, render)
        return c

    def _arm_retry(self):
        if not self._retry:
            return
        self._retry["armed"] = True
        self._set_retry_state("armed")
        self.add_sys(f"⏱ Armed — your message goes out when the allowance resets "
                     f"(about {self._retry_when()}). Type anything else and it stands down.")
        self._retry_tick()

    def _set_retry_state(self, state):
        btn = self._retry_btn
        if btn is not None:
            try:
                btn._set_ustate(state)
            except Exception:
                pass

    def _cancel_retry(self, note=None):
        """Stand down. Called on Clear, on a manual send, when the draft stops matching, and
        from the button itself — anything that means the user has taken the wheel back."""
        if self._retry_after is not None:
            try:
                self.root.after_cancel(self._retry_after)
            except Exception:
                pass
            self._retry_after = None
        was_armed = bool((self._retry or {}).get("armed"))
        if self._retry is not None:
            self._set_retry_state("off")
        self._retry = None
        self._retry_btn = None
        if note and was_armed:
            self.add_sys(note)

    def _retry_tick(self):
        """Poll the wall clock while a retry is armed. Also the place the guards live, because
        an armed retry has to survive minutes or hours of the user doing other things."""
        if self._retry_after is not None:
            # Arming and the CLI's own "you're allowed again" can both land here; without
            # this, each would leave its own after() chain polling the same retry.
            try:
                self.root.after_cancel(self._retry_after)
            except Exception:
                pass
            self._retry_after = None
        r = self._retry
        if not r or not r.get("armed"):
            return
        if self._entry_text() != r["text"]:
            # They've started writing something else. Sending the old text now would push
            # their draft out from under them mid-sentence.
            self._cancel_retry(note="⏱ Auto-send stood down — the box has something newer in it.")
            return
        if (r.get("ready") or time.time() >= r["at"]) and not self.busy and not self._compacting:
            self._fire_retry()
            return
        self._retry_after = self.root.after(_RETRY_POLL_MS, self._retry_tick)

    def _fire_retry(self):
        """Send it. Disarms FIRST, and only once: if the allowance still refuses (a clock
        that disagrees with the server's by a minute is enough), the refusal offers a fresh
        button rather than spinning a retry loop nobody asked for."""
        self._retry["armed"] = False
        self._set_retry_state("sent")
        self._retry = None
        self._retry_btn = None
        self.add_sys("⏱ Allowance is back — sending your message.")
        self._send_or_stop()

    def _compact_advice(self):
        """What compacting would cost right now, in seconds. Quoting today's number is also
        the argument for not waiting: the fitted duration rises with the size of what's being
        summarized, so the same job only gets more expensive from here.

        Deliberately restricted to our own remembered runs — _compact_samples_from_transcripts
        reads a dozen files and this runs on the UI thread, where half a second of disk is a
        visible stall in a window that sits on top of the user's work."""
        eta = _compact_predict(_compact_history(), self._ctx_tokens) if self._ctx_tokens else None
        cost = f" (~{self._compact_elapsed(eta)})" if eta else ""
        return f"Compact now{cost} — it only gets slower as the window fills."

    # ── compaction animation (mirrors the Claude Code CLI's /compact spinner) ──
    def _start_compact_anim(self, payload=None):
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
        # The bar rides in the mono font so its cells stay aligned at any zoom, and holds a
        # steady accent while the sparkle/elapsed pulse around it (raised so its font+colour
        # win over "compact", which _compact_tick recolours every frame).
        self.chat.tag_configure("compact_bar", foreground=T["accent"], font=self.f_mono)
        self.chat.tag_raise("compact_bar")
        self.chat.insert("end", "\n")
        start = self.chat.index("end-1c")           # start of our (about-to-be-written) line
        self.chat.insert("end", " \n", "compact")
        self.chat.mark_set("compact_ln", start)
        self.chat.mark_gravity("compact_ln", "left")  # stays at the line start across rewrites
        self._compact_line = True
        self._compact_t0 = time.monotonic()
        self._compact_frame = 0
        # How big the thing being compacted is (the worker reads it off the last context
        # measurement), and therefore roughly how long this should take.
        pre = payload.get("pre_tokens") if isinstance(payload, dict) else None
        self._compact_pre = int(pre) if isinstance(pre, (int, float)) and pre > 0 else None
        own = _compact_history()
        self._compact_eta = _compact_predict(own, self._compact_pre)
        if len(own) < 2:
            # Under two runs of our own there's no line to fit, so the estimate above (if any)
            # ignores size. Go mine the CLI's transcripts for more.
            self._seed_compact_eta()
        self._set_status("compacting…")
        self._scroll_follow()
        self._compact_tick()

    def _seed_compact_eta(self):
        """Too few compactions of our own to fit one: fall back to the durations recorded in
        the CLI's own session logs (which include any the overlay already drove). Reading a
        dozen of them takes ~0.5s — nothing next to a compaction that runs for minutes, but
        far too long to block the UI — so it happens off-thread, and the banner runs on
        whatever we had until this lands."""
        pre = self._compact_pre

        def work():
            try:
                samples = _compact_samples_from_transcripts()
                eta = _compact_predict(samples, pre) if len(samples) >= 2 else None
            except Exception:
                return
            if eta is not None:
                self.ui_q.put(("compact_eta", eta))
        threading.Thread(target=work, name="compact-eta", daemon=True).start()

    def _compact_tick(self):
        if not self._compacting or not self._compact_line:
            return
        frames = "✶✷✸✹✺✹✸✷"             # a sparkle that pulses (same ✦/✻ family as the rest of the UI)
        i = self._compact_frame
        spark = frames[i % len(frames)]
        t = time.monotonic() - self._compact_t0
        eta = self._compact_eta
        if eta and t < eta:
            frac = self._compact_progress(t, eta)
            # The percentage already carries the elapsed/predicted pair (it IS elapsed over the
            # prediction), so showing both said the same thing twice and wrapped the line.
            label, bar = "Compacting conversation", self._compact_bar_filled(frac)
            tail = f"   {frac * 100:.0f}%"
        else:
            # No prediction, or one we've already run past. Either way there's no honest
            # percentage left — a bar creeping 97 → 98 % looks hung and reads as *worse* than
            # admitting we don't know. So say so, circulate the band again, and put back the
            # one number we actually measured.
            label = "Still compacting" if eta else "Compacting conversation"
            bar, tail = self._compact_bar(i), f"   {self._compact_elapsed(t)}"
        try:
            self.chat.delete("compact_ln", "compact_ln lineend")
            self.chat.insert("compact_ln", f"{spark}  {label}   ", "compact",
                             bar, ("compact", "compact_bar"),
                             tail, "compact")
            self.chat.tag_configure(
                "compact", foreground=(T["accent"] if (i // 2) % 2 == 0 else T["accent_hi"]))
        except tk.TclError:
            return                        # line/mark gone (chat cleared) → stop quietly
        self._compact_frame = i + 1
        self._compact_anim_after = self.root.after(110, self._compact_tick)

    # /compact emits no progress events, so a *measured* percentage is impossible. Where we
    # have past timings the bar fills against a PREDICTED duration; where we don't — or once
    # a run outlives that prediction — a band circulates the track instead, which is honest
    # "working, duration unknown" motion rather than a number pretending to still mean something.
    # 18 cells at the 110ms tick means one full lap every ~2s, and each cell is ~5.5%.
    _COMPACT_BAR_CELLS = 18
    _COMPACT_BAR_BAND = 6

    def _compact_bar_filled(self, frac):
        # Floor, not round: _compact_progress never returns 1.0, so flooring guarantees the
        # last cell stays empty until the CLI actually reports done — a visually FULL bar
        # would claim a completion we haven't been told about.
        w = self._COMPACT_BAR_CELLS
        n = max(0, min(w, int(frac * w)))
        return "█" * n + "░" * (w - n)

    @staticmethod
    def _compact_progress(elapsed, eta):
        """Fraction of the predicted duration, 0 ≤ f ≤ 0.90 — linear, and capped short of the
        end because only the CLI's done event proves a compaction actually finished. Callers
        only use this up to the prediction; past it they drop the percentage rather than
        invent more of it."""
        if eta <= 0:
            return 0.0
        return 0.90 * max(0.0, min(1.0, elapsed / eta))

    def _compact_bar(self, frame):
        w, b = self._COMPACT_BAR_CELLS, self._COMPACT_BAR_BAND
        cells = ["░"] * w
        for k in range(b):
            cells[(frame + k) % w] = "█"
        return "".join(cells)

    @staticmethod
    def _compact_elapsed(sec):
        """Bare seconds while short; m+s once a compaction runs past a minute."""
        sec = max(0, int(sec))
        return f"{sec}s" if sec < 60 else f"{sec // 60}m{sec % 60:02d}s"

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
        took = time.monotonic() - self._compact_t0
        if status == "ok":
            # The CLI's own duration_ms is authoritative (it excludes our queueing); the wall
            # clock is the fallback. Remembering (size, duration) is what lets the NEXT
            # compaction show a progress bar instead of a bare spinner.
            m = meta if isinstance(meta, dict) else {}
            ms = m.get("duration_ms")
            if isinstance(ms, (int, float)) and ms > 0:
                took = ms / 1000.0
            _compact_history_add(m.get("pre_tokens") or self._compact_pre, took)
            final = self._format_compact_result(meta, took)
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

    def _format_compact_result(self, meta, took=None):
        # The duration isn't decoration: it's the sample the next run's estimate is built on,
        # so showing it lets the user see the prediction converge.
        el = f" in {self._compact_elapsed(took)}" if took else ""
        if isinstance(meta, dict) and meta.get("pre_tokens") and meta.get("post_tokens"):
            try:
                pre, post = int(meta["pre_tokens"]), int(meta["post_tokens"])
                saved = (1 - post / pre) * 100 if pre else 0
                return (f"✦ Compacted{el} — {pre:,} → {post:,} tokens "
                        f"(saved {saved:.0f}%). History summarized; keep going.")
            except Exception:
                pass
        return f"✦ Compacted{el} — conversation history summarized; keep going."

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
        self._sync_follow()          # scrolling up stops the follow; back to the end resumes it
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
        # Credential watchdog: the CLI's login can be killed from outside this process (a
        # failed refresh in ANY claude process blanks the shared credential file), and it can
        # be repaired from outside too (a terminal `claude auth login`). Poll it so both are
        # announced on their own. Throttled to AUTH_CHECK_INTERVAL — one stat() per tick.
        self._auth_watchdog(self._last_pump)
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
            self._discard_pending = False   # worker confirmed the wipe: stale events, if any,
                                            # have drained ahead of this; the new session is live
            self.add_sys("🔄 new conversation.")
            # Don't null _ctx_pct here: reset() already cleared it on click, and the worker's
            # post-_open _emit_usage has (just before this) pushed the NEW session's real
            # baseline. Nulling now would discard that correct value and leave a bare "—".
            self._refresh_statusline()
            self._set_busy(False)
            self._forget_sent_shots()        # fresh context has no previous screenshot
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
            # The worker schedules its usage refresh AFTER turn_done (to free "thinking…" a
            # round-trip earlier), so the reading that actually includes the finished turn is
            # this one — sample here rather than at turn_done, where it's a turn stale.
            if self._ctx_sample_due:
                self._ctx_sample_due = False
                self._note_ctx_turn()
        elif kind == "ctx_tokens":
            self._ctx_tokens = payload
        elif kind == "quota":
            self._quota = payload if isinstance(payload, dict) else None
            self._refresh_statusline()
            self._maybe_explain_ring()
            self._announce_quota()
            # The CLI saying the allowance is no longer rejected beats waiting for a clock we
            # only ever got a prediction of. A retry is only ever armed after a rejection, so
            # any later reading that isn't one means the window really has reopened.
            r = self._retry
            if r and r.get("armed") and (self._quota or {}).get("status") not in (None, "rejected"):
                r["ready"] = True     # sticky: if we're mid-turn right now, the next tick uses it
                self._retry_tick()
        elif kind == "quota_poll":
            # usage.py asked the allowance endpoint itself, so the gauge is right without
            # having to send a message first. Display only, and on purpose: no announcement
            # (the reading carries no status to announce) and no retry re-arm (only the CLI's
            # own event may decide that a refused message can go back on the wire).
            if isinstance(payload, dict):
                self._quota_polled = payload
                self._refresh_statusline()
                self._maybe_explain_ring()
        elif kind == "turn_done":
            self._md_finalize()          # the turn ended → give the last line full block styling
            self._finish_turn_copy()     # then a Copy button under the reply
            self._ctx_sample_due = True  # arm one burn-rate sample for the usage refresh coming
            self._set_busy(False)
            self._maybe_flag_done()      # badge the orb if this finished while collapsed
            # Whatever pending shot hashes weren't promoted by a clean "result" belong to a
            # turn that ended without one (stopped, errored, transport died) — the model may
            # never have seen those images, so they must not become dedupe baselines.
            self._pending_shot_hashes.clear()
            if not self._discard_pending:
                self._persist_session()  # this conversation is now the resumable one
                                         # (skipped for a turn Cleared mid-flight)
        elif kind == "session":
            if not self._discard_pending:   # ignore a stale id from a Cleared conversation
                self._session_id = str(payload)
        elif kind == "resumed":
            self._set_status("")
            if self._resume_btn is not None:
                try:
                    self._resume_btn._set_ustate("done")
                except Exception:
                    pass
                self._resume_btn = None
            self.add_sys("↺ Resumed your last conversation. The transcript isn't "
                         "replayed here, but Claude remembers it — just keep going.")
            self._persist_session()      # keep it resumable even if no new turn follows
        elif kind == "resume_failed":
            self._set_status("")
            self._forget_sent_shots()    # whatever session we're on, it isn't the one the
                                         # dedupe cache was describing
            if self._resume_btn is not None:
                try:
                    self._resume_btn._set_ustate("failed")
                except Exception:
                    pass
                self._resume_btn = None
            else:
                # The offer was already retired (a send or Clear raced the resume outcome),
                # so the button can't carry the news. Say it in the chat, or the fallback to
                # a fresh session would be completely silent — the one thing this feature is
                # meant not to do.
                self.add_sys("↺ Couldn't resume the previous conversation — this is a "
                             "fresh session.")
        elif kind == "resume_lost":
            # The connect looked like a resume, but the CLI's first streamed id proves it
            # silently started FRESH (see worker._set_session). Correct the earlier
            # optimistic "resumed" so the user isn't told the context is back when it isn't.
            self.add_err("⚠ The previous conversation couldn't be restored after all — the "
                         "CLI started a fresh session, so earlier context isn't available.")
            self._forget_sent_shots()    # the fresh session has none of the old screenshots
            if self._resume_btn is not None:
                try:
                    self._resume_btn._set_ustate("failed")
                except Exception:
                    pass
                self._resume_btn = None
        elif kind == "compacting":
            self._start_compact_anim(payload)
        elif kind == "compact_eta":
            # Size-aware estimate mined from the CLI's transcripts, arriving a beat into the run
            # (see _seed_compact_eta). It's fitted to two or more samples, so it supersedes the
            # size-blind median we may have started with — but only while the run is still live.
            if self._compacting:
                self._compact_eta = payload
        elif kind == "compact_done":
            self._stop_compact_anim(payload)
            # Compaction may summarize the previous screenshot right out of the context;
            # "screen unchanged, keep using it" would then point at nothing. Attach fresh.
            self._forget_sent_shots()
        elif kind == "auto_compacted":
            # Same as compact_done, but for the CLI's AUTOMATIC mid-stream compaction —
            # there is no banner/animation for it, only the cache consequence.
            self._forget_sent_shots()
        elif kind == "session_replaced":
            # The worker had to stand up a FRESH session (resume unsupported/failed): the
            # old context — including every screenshot in it — is gone.
            self._forget_sent_shots()
        elif kind == "error":
            self.add_err(str(payload))
            self._set_busy(False)
            # Any error may mean the turn (and its screenshot) never reached the model, or
            # that the worker reconnected into a fresh session. Deliberately coarse: the
            # cost of a wrong clear is one redundant image, the cost of a stale "unchanged"
            # pointer is the model trusting a screenshot it does not have.
            self._forget_sent_shots()
        elif kind == "result":
            self._md_finalize()          # finalize before any error line is appended
            self._finish_turn_copy()     # Copy button under whatever reply text we did get
            # the SDK reports a turn that ended in error here even when no exception was raised on
            # our side; surface it WITH the CLI's reason (subtype/result) instead of a generic line.
            if isinstance(payload, dict) and payload.get("is_error"):
                self.add_err(self._format_turn_error(payload))
                # A turn refused for allowance never reached Claude. Give the text back
                # rather than make the user reconstruct it after a wait they didn't choose.
                if "rate_limit" in str(payload.get("subtype") or "") and self._restore_draft():
                    self.add_sys("↩ Your message is back in the box.")
                    self._offer_retry((self._last_sent or ("", []))[0])
                # This turn's screenshots may never have reached the model — drop their
                # staged hashes so the next send re-attaches instead of saying "unchanged".
                # (Hashes already COMMITTED by earlier clean turns stay: an errored turn
                # doesn't remove images that are already part of the conversation.)
                self._pending_shot_hashes.clear()
            else:
                # Clean result: the model has verifiably seen this turn's images — they
                # become the dedupe baseline.
                self._sent_shot_hashes.update(self._pending_shot_hashes)
                self._pending_shot_hashes.clear()
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
        elif kind == "sessions":
            store, rows = payload
            self._show_session_rows(store, rows)
        elif kind == "sessions_failed":
            self._sessions_loading = False
            self.add_err(f"Couldn't read your past conversations: {payload}")
        elif kind == "status":
            self._set_status(str(payload))
        elif kind == "permission_mode":
            self._apply_permission_mode(str(payload))
        elif kind == "system":
            self.add_sys(str(payload))
        elif kind == "update":
            self._update_available = str(payload)
            self._show_overlay_update_notice(str(payload))
            self._refresh_statusline()
        elif kind == "ov_update_result":
            self._show_overlay_update_result(payload)
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
        try:
            if getattr(self, "_usage_poll", None):
                self._usage_poll.stop()  # daemon anyway; this just stops it waking up mid-teardown
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
    # Everything from here to mainloop() runs before there is a window to show an error
    # in, so a failure would otherwise be invisible (see crashreport). The guard writes
    # the traceback to %LOCALAPPDATA%\claude-overlay\crash.log and puts it on screen.
    with crashreport.guard("starting up", __version__):
        set_dpi_awareness()
        set_app_user_model_id()   # before any window, so the taskbar uses our icon
        _selfheal_taskbar_shortcut()
        try:
            Overlay().run()
        except KeyboardInterrupt:
            sys.exit(0)
