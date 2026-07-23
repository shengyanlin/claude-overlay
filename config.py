# -*- coding: utf-8 -*-
"""Configuration constants, theme palette, and the system-prompt append for
Claude Overlay. Pure data + small env helpers; no project imports (leaf module),
so anything may import it without a circular-import risk."""

import difflib
import json
import os
from pathlib import Path

__version__ = "1.13.0"

def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        val = int(os.environ.get(name, ""))
    except Exception:
        return default
    return max(min_value, min(max_value, val))



def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment so a machine can override a committed
    default WITHOUT editing source (used by STRICT_MCP_CONFIG below). Unset OR blank
    (empty / whitespace-only) -> default, so a stray space can't silently flip the
    flag; "0"/"false"/"no"/"off" (any case) -> False; anything else -> True."""
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v == "":
        return default
    return v not in ("0", "false", "no", "off")

WORKING_DIR = str(Path.home())
# Model IDs are FAMILY ALIASES ("opus"/"sonnet"/"haiku"), not pinned versions, so the
# overlay always runs the LATEST model of each family — when Anthropic ships a new one
# (e.g. a future Sonnet 5), it's picked up automatically with NO code change. The CLI
# documents these as "an alias for the latest model" (`claude --help`, see --model), and
# the "[1m]" 1M-context suffix composes with the alias too ("opus[1m]" → the latest Opus
# at 1M context). Don't use model=None: the Agent SDK resolves None to an OLDER model
# (SDK 0.2.87 → opus-4-7), not the CLI default, so we always pass an explicit alias.
# CAVEAT (measured 2026-07, CLI 2.1.156 / SDK 0.2.87): over the SDK's *streaming*
# transport — which is exactly how the overlay talks to the CLI — the CLI resolves a bare
# alias to a VERSION-BEHIND model (streaming "opus" → 4-7) even though its one-shot `-p`
# mode resolves the same alias to the latest (4-8). So we keep the alias HERE (to preserve
# auto-update on new releases) but the worker resolves it to the concrete latest id at
# startup via modelresolve.resolve_model (which probes the CLI's honest `-p` path once and
# caches the result, re-probing only when the CLI itself changes). The statusline shows the
# concrete version each alias resolved to
# (e.g. "claude-opus-4-8"), so you can always see what you're on.
MODEL = "opus"   # startup default: the latest Opus family
MODELS = [("Opus", "opus"), ("Opus (1M)", "opus[1m]"),
          ("Sonnet", "sonnet"), ("Haiku", "haiku")]  # click the statusline to switch
PERMISSION_MODE = "bypassPermissions"
                                 # the STARTUP permission mode; flip it at run time with the
                                 # status-bar "Read-only" toggle (◉ = "plan", a read-only agent
                                 # that can look and answer but not edit/run anything; ○ = back
                                 # to this configured mode). "plan" here starts the overlay
                                 # locked read-only. See the security note in README.md.
# Tools the overlay must NEVER let the model call, because they need an interactive UI
# this app can't provide. AskUserQuestion (Claude Code's structured multiple-choice
# question tool) is the one that bites: when the model calls it, the CLI blocks waiting
# for an answer that a GUI-with-no-TTY has no way to supply — so the turn hangs until the
# 30-min TOOL_IDLE_TIMEOUT fires (the "overlay froze on a question" bug). Disallowing it
# removes it from the tool schema entirely, so the model can't call it and instead asks
# its question inline as plain text — which the chat renders and you just type a reply to
# (the behaviour the overlay had before a CLI update started actively invoking the tool).
# Belt-and-suspenders: worker._allow_tool also DENIES it at run time, so even if it ever
# leaks back in (a skill, a future CLI that ignores this list) the turn can't hang.
DISALLOWED_TOOLS = ["AskUserQuestion"]
# Lean by default: do NOT inherit the user's ~/.claude MCP servers. The overlay is a
# lightweight screen-chat that only needs the core Claude Code tools; inheriting every
# MCP server the user has configured (Atlassian, Figma, M365, ...) injects their tool
# schemas into the context - easily 50-70K+ tokens, a third of a 200K window, gone
# before you type. Override per-machine WITHOUT editing source (so a release never has
# to toggle this constant): set CLAUDE_OVERLAY_STRICT_MCP=0 to inherit your MCP
# servers/connectors (incl. claude.ai Microsoft 365) for calendar/Outlook etc.
STRICT_MCP_CONFIG = _env_bool("CLAUDE_OVERLAY_STRICT_MCP", True)

SKILLS = "all"                    # which Agent SDK skills to enable in the overlay. Default None
                                  # means the overlay discovers NO skills (the SDK only wires up
                                  # skill discovery when this is set). A list enables ONLY those
                                  # skills by name — lean, just their description lands in context.
                                  # "all" enables every discovered skill (heavier: every skill's
                                  # description is injected). Setting this also makes the CLI load
                                  # ~/.claude settings (setting_sources defaults to user+project),
                                  # but NOT MCP servers (STRICT_MCP_CONFIG still blocks those).
                                  # None → skills off entirely.
AUTO_SCREENSHOT_DEFAULT = True
SHOW_IN_SCREEN_SHARE_DEFAULT = False  # False (default) = the overlay is excluded from screen
                                  # captures at the OS/DWM level (WDA_EXCLUDEFROMCAPTURE): it
                                  # stays visible to YOU but is omitted from Teams/Zoom/Meet/OBS
                                  # screen shares, PrintScreen, and our own screenshots — private.
                                  # True = the overlay shows up in screen shares. Flip it live via
                                  # the status-bar "shareable" toggle; no restart needed.
HIDE_SCREENSHOT_TOOL = True       # hide the noisy "⚙ Read …shot_*.png" lines every turn
HOTKEY = "ctrl+alt+space"
THEME = "light"                  # "light" (Claude paper) or "dark" (warm dark)
WINDOW_ALPHA = 1.0
CORNER_RADIUS = 18
TASKBAR_BUTTON = True            # show a real, clickable Windows taskbar button (with the
                                 # Clawd icon), like a normal app — alt-tab target, click to
                                 # focus/raise, see at a glance that it's running. The frameless
                                 # (overrideredirect) window gets NO taskbar button by default;
                                 # this forces one via WS_EX_APPWINDOW. False → the pure
                                 # no-taskbar floating overlay (original behaviour).
APP_ICON = "claude_overlay_2.ico"  # window + taskbar icon (Clawd). Path is relative to this
                                 # script (or absolute). "" → no custom icon (Tk default).
APP_ID = "shengyanlin.claude-overlay"  # explicit Windows AppUserModelID. Without it a pythonw
                                 # app shows pythonw's icon in the taskbar and groups with other
                                 # Python apps; setting it makes the taskbar use APP_ICON instead.
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
SHOT_FORMAT = os.environ.get("CLAUDE_OVERLAY_SHOT_FORMAT", "auto").strip().lower()
                                 # "auto" saves PNG + JPEG and keeps the smaller payload;
                                 # "png" preserves old behavior; "jpeg" favors upload speed.
SHOT_JPEG_QUALITY = _env_int("CLAUDE_OVERLAY_SHOT_JPEG_QUALITY", 82, 50, 95)
                                 # Claude downsamples larger images internally anyway, so
                                 # bigger files only cost upload time + vision tokens.
SHOT_SCOPE = os.environ.get("CLAUDE_OVERLAY_SHOT_SCOPE", "screens").strip().lower()
                                 # what a screenshot covers — the STARTUP default; flip it live
                                 # via the status-bar "Window-only" toggle. "screens" (default):
                                 # one image per monitor, Claude sees everything you see.
                                 # "window": ONLY the active (foreground) window — more private
                                 # and far cheaper in vision tokens, but Claude can't see other
                                 # windows/monitors. When you're typing IN the overlay, "active"
                                 # means the last window you worked in before it (tracked live);
                                 # when no usable window exists (fresh launch, desktop focused,
                                 # window minimized) it falls back to full-screen capture rather
                                 # than sending nothing. Any other value → "screens".
SHOT_SCOPE_FORCED = "CLAUDE_OVERLAY_SHOT_SCOPE" in os.environ
                                 # an EXPLICIT env var is a per-launch decision, so it beats the
                                 # remembered toggle state below; unset → last toggle choice wins
STATE_FILE = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "claude-overlay" / "state.json"
                                 # tiny per-machine store for UI-toggle state the user expects to
                                 # survive a relaunch (Window-only and Read-only). Deliberately
                                 # OUTSIDE the app folder: machine state must not dirty the git
                                 # clone or ride along in updates. SHOT_SCOPE / PERMISSION_MODE
                                 # seed the very first launch; after that the remembered toggle
                                 # wins — and because a restored Read-only choice is a SAFETY
                                 # state, the overlay announces it in-chat whenever it differs
                                 # from the configured default.
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
COMPACT_IDLE_TIMEOUT = 600  # /compact is one big summarization round-trip that streams nothing
                            # for a while (≈30s even on a small context); bound it generously
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
CLI_UPDATE_CHECK = _env_bool("CLAUDE_OVERLAY_CLI_UPDATE_CHECK", True)   # on launch, check whether
                            # the `claude` CLI the overlay drives is behind the latest npm release
                            # and, if so, show a one-click Update notice in the chat (see
                            # cliupdate.py). The overlay and the CLI update independently: keeping
                            # the overlay current never advances the CLI, and an old CLI silently
                            # runs an older model. Set CLAUDE_OVERLAY_CLI_UPDATE_CHECK=0 to disable
                            # (e.g. a locked-down box where global npm installs aren't allowed)

SYSTEM_APPEND = (
    "You are running as an always-on-top floating overlay assistant on the user's "
    "Windows 11 desktop. The user talks to you without leaving their current app. "
    "Messages may include live screenshots of the user's screen — attached directly "
    "as images, or (legacy) as an [ATTACHMENTS] path you open with the Read tool. "
    "Use them to see what the user is looking at, then help. "
    "Keep replies concise and skimmable since they render in a small floating window; "
    "expand only when asked. "
    "If you need to ask the user something, ask it inline as plain text and wait for "
    "their typed reply. This overlay is a plain chat with no interactive question UI, so "
    "never use a structured multiple-choice question tool — such a tool has no way to be "
    "answered here and would just stall the turn. "
    "When automating Office (PowerPoint/Excel/Word) via PowerShell+COM, optimize for "
    "speed: a NEW PowerShell process runs per tool call and COM state does NOT persist "
    "across calls, and every property access is a slow cross-process round-trip. So: "
    "(1) BATCH — do all inspection in ONE script (return what you need, e.g. as JSON), "
    "then apply ALL edits in ONE script; never one tool call per shape/cell/slide. "
    "(2) Within a script cache COM references in variables (grab the slide/shape/table "
    "once) instead of re-walking the object model, and don't re-read everything to verify "
    "after each write. (3) For Excel bulk writes, set Application.ScreenUpdating=$false, "
    "Calculation=xlManual and EnableEvents=$false around them, then restore. (4) For large "
    "purely-textual edits where the live open document isn't needed, python-pptx/openpyxl "
    "on the file is far faster than COM — but only when the file is NOT open in Office."
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

# ── per-machine overrides: config.json ──────────────────────────────────────
# Every constant above is a COMMITTED default — editing this file to taste dirties the
# git clone and turns every `git pull` into a conflict (or forces a rebase dance). So
# personal settings go in a tiny JSON file OUTSIDE the repo instead, in the same
# per-machine home that already holds the toggle state (STATE_FILE):
#
#     %LOCALAPPDATA%\claude-overlay\config.json
#     e.g.  { "PERMISSION_MODE": "plan", "THEME": "dark" }
#
# (or set CLAUDE_OVERLAY_CONFIG to any path — also how the tests isolate themselves).
# Precedence, weakest → strongest: the constants above < config.json < an explicitly
# set CLAUDE_OVERLAY_* env var (a per-launch decision keeps its old rank). And exactly
# like the constants they replace, SHOT_SCOPE / PERMISSION_MODE from the file only SEED
# the very first launch — the remembered Window-only / Read-only toggle state still wins.
# Unknown keys and wrong-typed values are SKIPPED, never fatal: each problem lands in
# USER_CONFIG_WARNINGS and the overlay surfaces them in-chat at startup, so a typo can't
# silently launch a misconfigured (say, full-access) session.
USER_CONFIG_FILE = Path(os.environ.get("CLAUDE_OVERLAY_CONFIG")
                        or Path(os.environ.get("LOCALAPPDATA") or Path.home())
                        / "claude-overlay" / "config.json")
USER_CONFIG_WARNINGS: list = []   # human-readable; shown in-chat by the overlay at startup

_BAD = object()   # sentinel: a validator rejected the value


def _v_bool(v):
    return v if isinstance(v, bool) else _BAD   # JSON true/false only — "true" is a typo


def _v_str(v):
    return v if isinstance(v, str) and v.strip() else _BAD


def _v_choice(*allowed):
    """Case-insensitive membership, returning the CANONICAL spelling (the CLI wants
    "bypassPermissions", not whatever casing the user typed)."""
    def check(v):
        if isinstance(v, str):
            s = v.strip().lower()
            for a in allowed:
                if s == a.lower():
                    return a
        return _BAD
    return check


def _v_num(lo, hi, cast):
    """Clamp into [lo, hi] like _env_int does — a slightly-out-of-range number means
    'as far as it goes', not a typo worth rejecting. bool is an int; exclude it."""
    def check(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return cast(max(lo, min(hi, v)))
        return _BAD
    return check


def _v_str_list(v):
    if isinstance(v, list) and v and all(isinstance(s, str) and s.strip() for s in v):
        return list(v)
    return _BAD


def _v_skills(v):
    return v if v is None or v == "all" else _v_str_list(v)


def _v_dir(v):
    """WORKING_DIR: expand ~ and %VARS%, and require the directory to EXIST — the CLI
    is spawned with this as its cwd, and a bad cwd fails far less legibly than this."""
    if not (isinstance(v, str) and v.strip()):
        return _BAD
    p = os.path.expandvars(os.path.expanduser(v.strip()))
    return p if os.path.isdir(p) else _BAD


# Which constants config.json may override, and how each value is checked. Deliberately
# a whitelist: derived/structural values (STATE_FILE, SHOT_DIR, THEMES, MODELS, the
# MAX_* safety caps…) stay source-only.
_USER_CONFIG_KEYS = {
    # agent
    "WORKING_DIR": _v_dir,
    "MODEL": _v_str,
    "PERMISSION_MODE": _v_choice("bypassPermissions", "acceptEdits", "default", "plan"),
    "SKILLS": _v_skills,                       # "all" | ["name", …] | null
    "STRICT_MCP_CONFIG": _v_bool,
    "CLI_UPDATE_CHECK": _v_bool,
    # capture
    "AUTO_SCREENSHOT_DEFAULT": _v_bool,
    "SHOT_SCOPE": _v_choice("screens", "window"),
    "SHOT_FORMAT": _v_choice("auto", "png", "jpeg"),
    "SHOT_JPEG_QUALITY": _v_num(50, 95, int),
    "HIDE_SCREENSHOT_TOOL": _v_bool,
    # appearance / window
    "THEME": _v_choice("light", "dark"),
    "SHOW_IN_SCREEN_SHARE_DEFAULT": _v_bool,
    "TASKBAR_BUTTON": _v_bool,
    "HOTKEY": _v_str,
    "WINDOW_ALPHA": _v_num(0.3, 1.0, float),   # floor 0.3: an ~invisible window looks broken
    "CORNER_RADIUS": _v_num(0, 40, int),
    "ORB_SIZE": _v_num(24, 160, int),
    "FONT_SANS": _v_str_list,
    "FONT_SERIF": _v_str_list,
    "FONT_MONO": _v_str_list,
}

# Settings that ALSO have an env var: an explicitly set (non-blank) env var is a
# per-launch decision and beats the file — the same rank env overrides always had.
_ENV_BEATS_JSON = {
    "SHOT_FORMAT": "CLAUDE_OVERLAY_SHOT_FORMAT",
    "SHOT_JPEG_QUALITY": "CLAUDE_OVERLAY_SHOT_JPEG_QUALITY",
    "SHOT_SCOPE": "CLAUDE_OVERLAY_SHOT_SCOPE",
    "STRICT_MCP_CONFIG": "CLAUDE_OVERLAY_STRICT_MCP",
    "CLI_UPDATE_CHECK": "CLAUDE_OVERLAY_CLI_UPDATE_CHECK",
}


def _perm_note(key):
    """When a skipped key is PERMISSION_MODE, spell out the mode still in force — the
    committed default is bypassPermissions (full access), so a typo'd read-only intent
    would otherwise fall through to a permissive session with only a generic warning."""
    if key != "PERMISSION_MODE":
        return ""
    mode = globals().get("PERMISSION_MODE")
    return f" — running in {mode!r}{', full access' if mode == 'bypassPermissions' else ''}"


def _apply_user_config():
    """Overlay USER_CONFIG_FILE onto the module constants, collecting a warning for
    everything skipped. Never raises: a broken config file must degrade to the
    committed defaults, not kill the app at import time."""
    try:
        if not USER_CONFIG_FILE.is_file():
            return
        if USER_CONFIG_FILE.stat().st_size > 64 * 1024:   # sanity cap, like STATE_FILE
            USER_CONFIG_WARNINGS.append("file is implausibly large — ignored.")
            return
        # utf-8-sig, not utf-8: Notepad and PowerShell Out-File/Set-Content default to a
        # UTF-8 BOM on Windows; plain "utf-8" leaves it in the text, json.loads then raises
        # and the WHOLE file is discarded — silently reverting PERMISSION_MODE to the
        # bypassPermissions default. "utf-8-sig" strips the BOM if present, no-op without.
        data = json.loads(USER_CONFIG_FILE.read_text("utf-8-sig"))
        if not isinstance(data, dict):
            USER_CONFIG_WARNINGS.append("top level must be a JSON object — ignored.")
            return
    except Exception as e:
        USER_CONFIG_WARNINGS.append(f"couldn't read it ({e}) — using the defaults.")
        return
    for key, val in data.items():
        check = _USER_CONFIG_KEYS.get(key)
        if check is None:
            # Suggest the intended key: a typo'd PERMISSION_MODE ("PERMISSIONS_MODE") is an
            # UNKNOWN key, so it can't be attributed to a specific setting — but "did you
            # mean 'PERMISSION_MODE'?" makes a mistyped safety-critical key just as legible
            # as a mistyped VALUE (which _perm_note already spells out).
            near = difflib.get_close_matches(str(key), _USER_CONFIG_KEYS, n=1)
            hint = f" — did you mean {near[0]!r}?" if near else " — ignored."
            USER_CONFIG_WARNINGS.append(f"unknown setting {key!r}{hint}")
            continue
        # Validate BEFORE the env-beats-file skip: an explicit env var outranks a VALID file
        # value silently (its old rank), but a typo'd/invalid file value must still be called
        # out even when an env var happens to shadow it — otherwise a bad setting vanishes
        # with no diagnostic, contradicting the "every skipped value is surfaced" contract.
        try:
            good = check(val)
        except Exception:
            # A validator that touches the OS (e.g. _v_dir → os.path.isdir) can raise on a
            # hostile value — on Windows a null byte gives "ValueError: embedded null byte".
            # Must degrade to a warning here: this loop runs at import, so a propagating
            # exception would break `from config import *` and the app wouldn't launch.
            good = _BAD
        if good is _BAD:
            USER_CONFIG_WARNINGS.append(
                f"invalid value for {key}: {val!r} — keeping the default{_perm_note(key)}.")
            continue
        env = _ENV_BEATS_JSON.get(key)
        if env and (os.environ.get(env) or "").strip():
            continue                       # valid, but an explicit env var outranks it this launch
        globals()[key] = good


_apply_user_config()

T = THEMES.get(THEME, THEMES["light"])
