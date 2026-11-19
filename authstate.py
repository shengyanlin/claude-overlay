# -*- coding: utf-8 -*-
"""Read-only view of the `claude` CLI's stored OAuth state, so the overlay can tell a
recoverable hiccup apart from "the CLI's login is gone and no message will ever work".

WHY THIS EXISTS
The overlay owns no credentials: the `claude` CLI it drives handles authentication, and
the overlay never sees a token. When the CLI's OAuth access token (8h) can no longer be
refreshed and the refresh is rejected with invalid_grant, the CLI does two things:

  1. marks that refresh token dead IN THE PROCESS that hit it (an in-memory set), and
  2. compare-and-swaps the on-disk credentials to {refreshToken:"", accessToken:"",
     expiresAt:0} — but ONLY if the file still holds the very token it just tried.

After (2), EVERY claude process — new ones included — fails with "Failed to authenticate:
OAuth session expired and could not be refreshed" until the user signs in again. Nothing
client-side can undo it: restarting the CLI, or the overlay, changes nothing.

Because (2) is a compare-and-swap, the opposite case exists too: when a sibling process
had already rotated the token, the disk is left FINE and only the process that lost the
race is poisoned (its in-memory dead-set). There, starting a fresh CLI subprocess is a
complete fix — which is what the worker's credential-signature recycle does.

This module is how the overlay tells those apart:
  * dead_reason()       — a reason string ONLY on positive, unambiguous proof that the
                          stored login is unusable, else None.
  * is_auth_error_text()— recognises the CLI's own auth-failure wording in an errored
                          turn, which is how we learn about it the moment it happens.
  * signature()         — cheap change-detector for the credential file, so a long-lived
                          subprocess holding a stale copy can be recycled.

SAFETY / DOCTRINE
A false "dead" would refuse to send perfectly good messages, so every check here is
one-sided: it fires only on the exact marker the CLI writes AND reads back as dead, it
stands down whenever any alternative credential source is configured, and the blocking
behaviour has an env-var escape hatch (config.AUTH_GATE / CLAUDE_OVERLAY_AUTH_GATE=0).
Absence of evidence is never treated as evidence: no file, unreadable file, secure-store
(keychain) backend, or an expiry merely in the past all return None.

Leaf module: stdlib only, no project imports. Every function degrades to None/False on
ANY failure and never raises into the caller.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_CRED_FILE = ".credentials.json"
_SETTINGS_FILE = "settings.json"
_MAX_CRED_BYTES = 256 * 1024        # the real file is ~1KB; never slurp something huge

# Env vars that mean this CLI run will NOT be authenticating with the stored OAuth login,
# so a blanked OAuth record says nothing about whether a message can be sent. Mirrors the
# CLI's own precondition: it raises the "OAuth session expired" error only when there is no
# API key, no auth token and no gateway in play. Any hit here → stand down.
_ALT_AUTH_ENV = (
    "ANTHROPIC_API_KEY",            # plain API-key auth
    "ANTHROPIC_AUTH_TOKEN",         # pre-minted bearer token
    "CLAUDE_CODE_OAUTH_TOKEN",      # long-lived token from `claude setup-token`
    "ANTHROPIC_BEDROCK_BASE_URL",   # Bedrock / Vertex / gateway deployments authenticate
    "ANTHROPIC_VERTEX_BASE_URL",    # through their own cloud credentials, not this file
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_GATEWAY_URL",
)

# The CLI's own wording for "this login is finished". Matched case-insensitively as
# substrings, and ONLY ever against text the CLI reported as an ERROR (an errored
# ResultMessage's detail, or a connect exception) — never against reply text, which could
# legitimately discuss OAuth and must not be mistaken for a broken login.
_AUTH_ERROR_MARKERS = (
    "oauth session expired",                    # "Failed to authenticate: OAuth session
                                                #  expired and could not be refreshed"
    "oauth refresh token is no longer valid",   # OAuthRefreshDeadError's message
    "oauthrefreshdeaderror",                    # ...and its class name, if it leaks raw
    "run /login",                               # the instruction both of those carry
    "claude auth login",                        # newer wording for the same fix
)


def config_dir() -> Path:
    """The CLI's config directory: $CLAUDE_CONFIG_DIR if set, else ~/.claude (the same
    resolution the CLI uses to find .credentials.json)."""
    d = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(d) if d else Path.home() / ".claude"


def credentials_path() -> Path:
    """Path the CLI keeps its OAuth tokens in. May legitimately NOT exist: on macOS (and
    with CLAUDE_SECURESTORAGE_CONFIG_DIR) the tokens live in an OS keychain instead."""
    return config_dir() / _CRED_FILE


def signature(block=False):
    """A cheap (mtime_ns, size) fingerprint of the credential file, or None if it can't be
    stat'ed. Used to detect "the tokens changed under a long-lived CLI subprocess" — a
    successful refresh by any process rewrites this file, and the subprocess that has been
    running since before that may still be holding the superseded copy. One stat() call, so
    it's safe on the send path. (macOS, where there is no file: a digest of the keychain
    record instead — see _keychain_blob — which changes on exactly the same occasions.)

    block=True is for taking a BASELINE, off the UI thread: on macOS it reads the keychain
    record now and waits for it, cache or no cache. The default hands back the cached copy,
    which is None until the first background read lands and may predate whatever the CLI
    stored a moment ago — fine for a later comparison, wrong for the value everything is
    compared against. No difference where the file exists: stat() is always current."""
    try:
        st = credentials_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        pass
    blob = _keychain_blob(block=block, max_age=0.0 if block else None)
    if blob:
        return ("keychain", hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest())
    return None


# ── macOS: the same record, in the login keychain ───────────────────────────────────────
# On a Mac the CLI doesn't write .credentials.json: it keeps the identical JSON in the login
# keychain, as a generic password it stores and reads with /usr/bin/security. Reading it the
# same way needs no prompt (the item's access list names `security`, which created it) and
# gives every check in this module the evidence it has on Windows — without it the Mac
# overlay would be blind to a dead login, and the allowance ring would stay empty.
#
# `security` is a process spawn, and dead_reason() runs on the UI thread, so the record is
# cached and refreshed in the background: a caller gets the last known copy at once and a
# stale one triggers a re-read for next time (block=True, for callers already off the UI
# thread, waits for a fresh one instead). A custom CLAUDE_CONFIG_DIR changes the item's
# name in ways this doesn't try to follow — that case stays "no evidence", as before.
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_SECURITY = "/usr/bin/security"
_KEYCHAIN_TTL = 30.0
_kc = {"at": None, "blob": None, "busy": False}
_kc_lock = threading.Lock()
# One `security` at a time, so reads land in the cache in the order they were made. Two
# overlapping ones could finish out of order and leave the OLDER record cached as the
# newest — which signature() would then report as the tokens changing, twice (there and
# back). Held across the spawn, so only ever taken off the UI thread.
_kc_read_lock = threading.Lock()


def _keychain_read():
    try:
        # By full path: what this prints is the login itself, and config.py has just put
        # the user's shell PATH — every folder a tool was ever installed into — in front.
        # A stray `security` there must not be the program that gets asked for it.
        r = subprocess.run([_SECURITY, "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, timeout=8, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not (0 < len(r.stdout) <= _MAX_CRED_BYTES):
            return None
        return r.stdout.decode("utf-8", "replace").strip() or None
    except Exception:
        return None


def _keychain_cached(max_age):
    """(fresh, blob) for the cached record. Caller holds _kc_lock."""
    at = _kc["at"]
    return at is not None and (time.monotonic() - at) < max_age, _kc["blob"]


def _keychain_refresh(max_age=0.0):
    with _kc_read_lock:
        if max_age > 0:             # the read we queued behind may be all this caller needed
            with _kc_lock:
                fresh, blob = _keychain_cached(max_age)
            if fresh:
                return blob
        blob = _keychain_read()
        with _kc_lock:
            _kc["blob"], _kc["at"], _kc["busy"] = blob, time.monotonic(), False
    return blob


def _keychain_blob(block=False, max_age=None):
    """The keychain's copy of the credential JSON (a string), or None: not macOS, a custom
    config dir, no such item, or not read yet. Never raises. max_age is how old a cached
    copy may be and still be served (default: _KEYCHAIN_TTL); block=True with max_age=0
    always re-reads."""
    if sys.platform != "darwin" or (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip():
        return None
    if max_age is None:
        max_age = _KEYCHAIN_TTL
    with _kc_lock:
        fresh, blob = _keychain_cached(max_age)
        if fresh:
            return blob
        if block:
            _kc["busy"] = True
        elif not _kc["busy"]:
            _kc["busy"] = True
            threading.Thread(target=_keychain_refresh, name="keychain-read",
                             daemon=True).start()
    return _keychain_refresh(max_age) if block else blob


def read_oauth(block=False):
    """The stored claudeAiOauth record wherever this platform keeps it — the credential
    file, else (macOS) the keychain — or None when there is nothing readable. block=True
    waits for a fresh keychain read; only for callers that are off the UI thread."""
    oauth = _read_oauth_file()
    if oauth is not None:
        return oauth
    blob = _keychain_blob(block=block)
    if not blob:
        return None
    try:
        data = json.loads(blob)
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        return oauth if isinstance(oauth, dict) else None
    except Exception:
        return None


def _read_oauth():
    """The stored claudeAiOauth record as a dict, or None when there is nothing readable
    (no file or keychain item, too big, bad JSON, different shape). None means "no
    evidence", never "bad"."""
    return read_oauth()


def _read_oauth_file():
    """The record from .credentials.json, or None."""
    try:
        p = credentials_path()
        if p.stat().st_size > _MAX_CRED_BYTES:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        return oauth if isinstance(oauth, dict) else None
    except Exception:
        return None


def alt_auth_configured() -> bool:
    """True when something other than the stored OAuth login could be authenticating this
    CLI (API key / token env var, Bedrock / Vertex / gateway, or an apiKeyHelper in
    settings.json). Fails SAFE: any doubt (unreadable settings) counts as no alternative,
    which only ever makes the caller more cautious about claiming the login is dead —
    dead_reason() still needs the positive on-disk marker before it says anything."""
    for name in _ALT_AUTH_ENV:
        v = (os.environ.get(name) or "").strip().lower()
        if v and v not in ("0", "false", "no", "off"):
            return True
    try:                                  # settings.json may configure an apiKeyHelper
        p = config_dir() / _SETTINGS_FILE
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            s = json.load(fh)
        if isinstance(s, dict) and str(s.get("apiKeyHelper") or "").strip():
            return True
    except Exception:
        pass
    return False


def dead_reason():
    """A one-line reason when the CLI's stored OAuth login is PROVABLY unusable, else None.

    The one condition that qualifies is the marker the CLI writes itself and reads back as
    dead: claudeAiOauth.refreshToken == "" (it blanks refreshToken + accessToken and zeroes
    expiresAt after an invalid_grant refresh failure). Deliberately NOT treated as dead:
      * an expired accessToken / expiresAt in the past — that is the normal state between
        refreshes and says nothing about whether the refresh will succeed;
      * refreshTokenExpiresAt in the past — not the CLI's own test, and the server is the
        only authority on whether a refresh token is still accepted;
      * no file / unreadable file / no claudeAiOauth key — the tokens may be in a keychain
        or another auth source may be in play, i.e. we have no evidence at all.
    One-sided on purpose: a wrong "dead" here would block messages that would have worked."""
    if alt_auth_configured():
        return None
    oauth = _read_oauth()
    if oauth is None:
        return None
    rt = oauth.get("refreshToken")
    if not isinstance(rt, str) or rt != "":       # exactly the blank marker, nothing else
        return None
    return "the Claude CLI cleared its stored login after a failed token refresh"


def is_auth_error_text(text) -> bool:
    """Whether CLI-reported ERROR text is the "your login is finished" failure. Call this
    only on error payloads (errored ResultMessage detail/subtype, connect exceptions) —
    matching it against ordinary reply text would misfire on any answer that discusses
    OAuth."""
    if not text:
        return False
    try:
        low = str(text).lower()
    except Exception:
        return False
    return any(m in low for m in _AUTH_ERROR_MARKERS)
