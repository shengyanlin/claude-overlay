# -*- coding: utf-8 -*-
"""Detect an out-of-date `claude` CLI and run a one-click, in-overlay update.

WHY THIS EXISTS
The overlay is a thin GUI over the `claude` CLI (driven through the Agent SDK). Which
models exist — and what a family alias like `opus` resolves to — is decided by the CLI
(and the backend), NOT the overlay: see modelresolve.py's alias-lag note. So an overlay
that's fully up to date but a CLI that's months behind silently runs an OLDER model, or
can't reach a brand-new one at all. And the CLI's own auto-updater does NOT run for a
global-npm install (the overlay's assumed setup — it needs %APPDATA%\\npm on PATH to find
`claude`), so the CLI can sit many versions behind indefinitely with no prompting.

WHAT
`check_update()` compares the installed CLI version against the latest published on the
npm registry and returns whether it's behind. When it is, the overlay shows a one-line
notice with an "Update" button; clicking it calls `run_update()`, which runs
`npm install -g @anthropic-ai/claude-code@latest`. Nothing is ever done silently or
without a click.

SAFETY / COST (mirrors modelresolve.py's doctrine)
Leaf module: stdlib only (modelresolve is imported lazily, inside a function, purely to
reuse its tested `claude -v` probe). Every function degrades to None / (False, reason) on
ANY failure (no npm, offline, corporate proxy block, odd output) so a check can neither
block nor break startup. The npm-registry lookup and `claude -v` are node cold starts of a
few seconds, so callers run this off the UI thread AND it's throttled to once/day via a
cache in ~/.claude-overlay — most launches read one small file and spawn nothing.

WINDOWS
npm ships as `npm.cmd`, a batch shim that CreateProcess can't exec directly, so every npm
invocation goes through `cmd /c` (bare `npm`, resolved off PATH — avoids quoting the space
in a path like "C:\\Users\\Lin Jason\\..."). CREATE_NO_WINDOW keeps a console from flashing
under pythonw. `npm view` prints the version to STDOUT and its "new npm available" nags to
STDERR, so the version is parsed from stdout ONLY (a stderr nag can contain a stray x.y.z)."""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PKG = "@anthropic-ai/claude-code"
_CACHE_PATH = Path.home() / ".claude-overlay" / "cli_update_cache.json"
_CHECK_INTERVAL_S = 24 * 60 * 60          # re-query the registry at most once/day
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_NPM_VIEW_TIMEOUT = 25                     # registry lookup (cache miss only)
_INSTALL_TIMEOUT = 300                     # `npm install -g` (download + extract)
_REASON_MAX = 200                          # cap a surfaced failure reason


def _find_npm():
    """Path to the npm launcher if npm is on PATH, else None — a cheap gate so we never even
    try to check/update when npm isn't installed (a native-installer setup, say). shutil.which
    honours PATHEXT, so it finds npm.cmd on Windows."""
    return shutil.which("npm")


def _run(args, timeout):
    """Run `cmd /c <args>` capturing stdout+stderr as decoded UTF-8. Going through cmd.exe is
    required on Windows to launch npm (a .cmd shim); CREATE_NO_WINDOW suppresses the console
    under pythonw. Returns (returncode, stdout, stderr) or None if it couldn't even spawn.
    Never raises."""
    try:
        r = subprocess.run(["cmd", "/c", *args], capture_output=True, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
    except Exception:
        return None
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    return (r.returncode, dec(r.stdout), dec(r.stderr))


def _parse_ver(s):
    """'2.1.198' -> (2, 1, 198) for a correct numeric compare ((2,1,198) < (2,1,204)). Caps each
    field's digits so a hostile 'version' can't hit Python's int-from-string limit; missing ->
    (0,) so an unreadable version sorts oldest (never spuriously 'ahead')."""
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(n[:9]) for n in nums[:3]) if nums else (0,)


def installed_version():
    """The installed CLI's version string (e.g. '2.1.198') or None. Reuses modelresolve's tested
    `claude -v` probe (imported lazily) so there's ONE source of truth for what the CLI reports."""
    try:
        import modelresolve
        return modelresolve.cli_version()
    except Exception:
        return None


def _npm_latest():
    """The latest published version of the package on the npm registry (the exact version
    `npm install -g <pkg>@latest` would install), or None. Parses STDOUT only — npm's upgrade
    nags go to stderr and could otherwise be mis-read as the package version."""
    res = _run(["npm", "view", _PKG, "version"], _NPM_VIEW_TIMEOUT)
    if not res:
        return None
    rc, out, _err = res
    if rc != 0:
        return None
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", out)
    return m.group(1) if m else None


def _load_cache():
    try:
        c = json.loads(_CACHE_PATH.read_text("utf-8"))
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def _save_cache(result):
    """Persist a check result with a timestamp so the (slow) registry lookup is skipped for
    _CHECK_INTERVAL_S. Best-effort — a write failure just means we re-check next launch."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({"ts": time.time(), "result": result}), "utf-8")
    except Exception:
        pass


def _fresh_cache():
    """The cached check result if it's younger than _CHECK_INTERVAL_S and well-formed, else None."""
    c = _load_cache()
    if not c:
        return None
    ts = c.get("ts")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > _CHECK_INTERVAL_S:
        return None
    r = c.get("result")
    if isinstance(r, dict) and {"installed", "latest", "behind"} <= set(r):
        return r
    return None


def check_update(force=False):
    """Decide whether the installed CLI is behind the latest npm release.

    Returns {"installed": "2.1.198", "latest": "2.1.204", "behind": True} — or None on ANY
    failure (no npm, offline, proxy block, unparseable output) so the caller simply shows no
    notice. Throttled: a cache newer than _CHECK_INTERVAL_S short-circuits the slow node/npm
    calls (pass force=True to bypass, e.g. right after an update)."""
    if _find_npm() is None:
        return None
    if not force:
        cached = _fresh_cache()
        if cached is not None:
            return cached
    inst = installed_version()
    latest = _npm_latest()
    if not inst or not latest:
        return None
    result = {"installed": inst, "latest": latest,
              "behind": bool(_parse_ver(inst) < _parse_ver(latest))}
    _save_cache(result)
    return result


def _failure_reason(rc, out, err):
    """A short, USER-FACING reason for a failed `npm install -g`. Two Windows realities drive this:

    (1) The COMMON, structural failure is EBUSY / EPERM: the `claude.exe` npm is trying to replace
        is LOCKED because a Claude process is running — most often the overlay's OWN worker (the
        window hosting this button is itself running `claude`), but also any other Claude Code
        window. Windows can't overwrite a running exe. So detect it and say so ACTIONABLY, rather
        than leaking npm's raw error.
    (2) Otherwise, npm's LAST stderr line is the useless "A complete log of this run can be found
        in: …" pointer — skip it and surface the real error line, trimming the "npm error " prefix."""
    blob = (err or "") + "\n" + (out or "")
    low = blob.lower()
    if any(k in low for k in ("ebusy", "resource busy or locked", "eperm", "text file busy")):
        return ("a running Claude process is using the CLI, so it can't be replaced — close other "
                "Claude windows (including this overlay) and try again")
    if "eacces" in low or "permission denied" in low:
        return "permission denied writing the global npm folder"
    for line in reversed(blob.splitlines()):
        s = line.strip().rstrip(".")
        if not s or "a complete log of this run" in s.lower():
            continue
        s = re.sub(r"^npm (error|err!)\s*", "", s, flags=re.IGNORECASE).strip()
        if s:
            return s[:_REASON_MAX]
    return f"npm exited with code {rc}"


def run_update():
    """Run `npm install -g @anthropic-ai/claude-code@latest`.

    Returns (True, new_version) on success — after which the cache is refreshed to not-behind
    so the notice won't re-fire — or (False, short_reason) on failure. Never raises; the caller
    can always fall back to running the npm command by hand."""
    if _find_npm() is None:
        return (False, "npm not found on PATH")
    res = _run(["npm", "install", "-g", _PKG + "@latest"], _INSTALL_TIMEOUT)
    if not res:
        return (False, "couldn't launch npm")
    rc, out, err = res
    if rc != 0:
        return (False, _failure_reason(rc, out, err))
    new = installed_version() or "latest"
    _save_cache({"installed": new, "latest": new, "behind": False})
    return (True, new)
