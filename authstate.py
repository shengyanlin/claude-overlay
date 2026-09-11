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

import json
import os
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


def signature():
    """A cheap (mtime_ns, size) fingerprint of the credential file, or None if it can't be
    stat'ed. Used to detect "the tokens changed under a long-lived CLI subprocess" — a
    successful refresh by any process rewrites this file, and the subprocess that has been
    running since before that may still be holding the superseded copy. One stat() call, so
    it's safe on the send path."""
    try:
        st = credentials_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def _read_oauth():
    """The stored claudeAiOauth record as a dict, or None when there is nothing readable
    (no file, too big, bad JSON, different shape). None means "no evidence", never "bad"."""
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
