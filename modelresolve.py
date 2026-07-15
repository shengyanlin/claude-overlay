# -*- coding: utf-8 -*-
"""Resolve a Claude model FAMILY ALIAS to the concrete latest model id.

WHY THIS EXISTS
The overlay drives the CLI through the Agent SDK's *streaming* (stream-json)
transport. Measured 2026-07 (CLI 2.1.156, SDK 0.2.87): in that streaming mode the
CLI resolves a bare family alias to an OLDER model than its one-shot `-p` mode does —
`--model opus` over streaming → claude-opus-4-7, but `claude --model opus -p ...` →
claude-opus-4-8. So passing the bare alias to the streaming session silently runs a
version-behind model (the statusline showed "opus 4.7" while `-p` and Claude Code
itself were on 4.8). The `claude --help` text for --model even PROMISES "an alias for
the latest model" — a promise the streaming path quietly breaks.

FIX
Ask the CLI's own `-p` path (which honours "latest") what the alias resolves to, then
hand the streaming session that concrete id. This KEEPS config.py's "always the latest
of the family, no code edits on a new release" design — when Anthropic ships a newer
model the probe re-resolves to it automatically — while dodging the streaming lag.

COST
A probe is one short `-p` turn. We cache the alias->id result keyed by the CLI launcher's
file signature, so a cache hit costs nothing and a CLI upgrade forces an immediate
re-probe. The map is NOT purely a function of the CLI binary, though: the `-p` path
resolves an alias using the account's model *entitlement* and the org's default model
(server-side state — e.g. a managed org enabling a newer Sonnet mid-week), which can
change with NO CLI upgrade and so no signature change. So the cache also carries a
timestamp and is re-probed at least every _CACHE_TTL_S; that bounds how long such a silent
server-side change can leave a stale id, while keeping almost every launch free. (A cache
written by a pre-TTL overlay has no timestamp and is treated as stale, so it self-heals on
the first launch after upgrade.)

SAFETY
Leaf module: stdlib only (no project imports), and every function degrades to returning
the ORIGINAL spec on any failure (offline, not logged in, CLI missing, odd output) so
resolution can never block or break startup — worst case the overlay behaves exactly as
it did before this module existed (the bare alias)."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Bare family aliases the overlay ships in config.MODELS, optionally with the "[1m]"
# 1M-context suffix. Only these get resolved; a concrete id / "inherit" / anything else
# passes through untouched.
_ALIAS_RE = re.compile(r"^(opus|sonnet|haiku)(\[1m\])?$", re.IGNORECASE)
_CACHE_PATH = Path.home() / ".claude-overlay" / "model_cache.json"
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_PROBE_TIMEOUT = 30      # seconds for the one-shot -p resolve turn (cache miss only)
_VERSION_TIMEOUT = 8
# Re-probe at least this often even when the CLI launcher hasn't changed: a family alias's
# concrete id also depends on server-side state (account model entitlement + the org's
# default model) that can flip with NO CLI upgrade, which the file-signature key alone
# can't detect. This bounds staleness to a few hours while keeping almost every launch free.
_CACHE_TTL_S = 6 * 3600


def _find_cli():
    """Same discovery the SDK's transport uses (shutil.which('claude')), so we probe the
    exact CLI the streaming session will spawn. Returns a path string or None."""
    return shutil.which("claude")


def _run_cli(cli, args, timeout):
    """Run the CLI capturing BYTES and decode UTF-8 ourselves. Never pass text=True: the
    CLI emits UTF-8, but Python would decode with the locale codec (e.g. cp950 on a zh-TW
    Windows box), and a non-ASCII byte then raises UnicodeDecodeError and drops stdout
    entirely. creationflags keeps a console from flashing under pythonw. Returns decoded
    stdout on a clean (rc 0) run, else None. Never raises."""
    try:
        r = subprocess.run([cli, *args], capture_output=True, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or b"").decode("utf-8", "replace")


def cli_signature(cli):
    """A cheap fingerprint of the installed CLI — the launcher's (resolved path, size,
    mtime) — used as the cache key. Package managers (npm/pnpm/yarn) rewrite the launcher
    shim on install/upgrade, so a changed signature means "the CLI changed, re-resolve".
    Crucially this is a stat() (microseconds), NOT `claude -v` (a node cold start of a few
    SECONDS) — so a cache hit costs nothing perceptible on every launch. Returns a string
    or None; on None the caller re-probes (correctness over speed)."""
    try:
        st = os.stat(cli)
        return f"{cli}|{st.st_size}|{st.st_mtime_ns}"
    except Exception:
        return None


def cli_version(cli=None):
    """The CLI's version string (e.g. '2.1.156'), or None. Not used for caching (that's
    cli_signature) — kept as a small, tested helper for diagnostics."""
    cli = cli or _find_cli()
    if not cli:
        return None
    out = _run_cli(cli, ["-v"], _VERSION_TIMEOUT)
    if not out:
        return None
    m = re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", out.strip())
    return m.group(1) if m else (out.strip()[:40] or None)


def _probe_concrete(cli, base_alias):
    """Resolve a bare family alias to its concrete latest id via the CLI's `-p` path (the
    one that honours 'latest'). Returns e.g. 'claude-opus-4-8', or None on any failure.

    The alias->id map is baked into the CLI and is independent of MCP servers, settings, and
    CLAUDE.md — so strip all of that from the probe (--strict-mcp-config so the user's heavy
    MCP servers aren't loaded, empty --setting-sources so user/project settings + memory
    discovery are skipped). Without this the probe can take ~20s on a box with many MCP
    servers; with it, a few seconds."""
    out = _run_cli(cli, ["--model", base_alias, "-p", "ok", "--output-format", "json",
                         "--strict-mcp-config", "--setting-sources="],
                   _PROBE_TIMEOUT)
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    usage = data.get("modelUsage") if isinstance(data, dict) else None
    if isinstance(usage, dict) and usage:
        keys = list(usage.keys())
        fam = base_alias.lower()
        # A turn can bill a small helper model (e.g. haiku for a title) alongside the main
        # one, so prefer the key whose family matches the alias we asked for.
        for k in keys:
            if fam in k.lower():
                return k
        if len(keys) == 1:
            return keys[0]
    # Fall back to a top-level "model" field if the shape ever changes.
    m = data.get("model") if isinstance(data, dict) else None
    return m if isinstance(m, str) and m else None


def _load_cache():
    try:
        c = json.loads(_CACHE_PATH.read_text("utf-8"))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _save_cache(cache):
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache), "utf-8")
    except Exception:
        pass


def _cache_is_fresh(cache, sig):
    """True only if the cached alias map is still trustworthy: it was built against the
    CURRENT CLI signature AND within the TTL. A cache from a different CLI (signature
    mismatch), from a pre-TTL overlay (no 'probed_at' timestamp), or older than
    _CACHE_TTL_S is stale — so a server-side entitlement / org-default change that didn't
    bump the CLI signature still gets picked up on the next probe."""
    if not isinstance(cache, dict) or sig is None:
        return False
    if cache.get("signature") != sig:
        return False
    at = cache.get("probed_at")
    if not isinstance(at, (int, float)):
        return False                      # pre-TTL cache (no timestamp) -> treat as stale
    return (time.time() - at) <= _CACHE_TTL_S


def resolve_model(spec, use_cache=True):
    """Map a model spec to the concrete latest id the streaming session should use.

    - Bare family alias ('opus', 'opus[1m]', 'sonnet', 'haiku') -> the concrete latest id
      ('claude-opus-4-8'), re-attaching a '[1m]' suffix if the spec had one.
    - Anything else (already-concrete id, 'inherit', unknown) -> returned unchanged.
    - ANY failure (no CLI, offline, probe error) -> the ORIGINAL spec, so resolution can
      never block or break startup; the overlay just behaves as it did before."""
    if not isinstance(spec, str) or not spec.strip():
        return spec
    m = _ALIAS_RE.match(spec.strip())
    if not m:
        return spec                       # concrete id / 'inherit' / unknown -> pass through
    base = m.group(1).lower()
    suffix = "[1m]" if m.group(2) else ""
    cli = _find_cli()
    if not cli:
        return spec
    sig = cli_signature(cli)
    cache = _load_cache() if use_cache else {}
    fresh = _cache_is_fresh(cache, sig)   # current CLI signature AND within the TTL
    if use_cache and fresh:
        hit = (cache.get("aliases") or {}).get(base)
        if hit:
            return hit + suffix           # fast path: no subprocess at all
    concrete = _probe_concrete(cli, base)
    if not concrete:
        return spec                       # probe failed -> behave exactly as before
    if use_cache and sig is not None:
        if not fresh:
            # CLI changed, cache aged out, or pre-TTL cache -> start a fresh generation
            # stamped now. (Adding an alias to an already-fresh generation below keeps the
            # original timestamp, so switching models can't extend the TTL indefinitely.)
            cache = {"signature": sig, "probed_at": time.time(), "aliases": {}}
        cache.setdefault("aliases", {})[base] = concrete
        _save_cache(cache)
    return concrete + suffix
