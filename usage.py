# -*- coding: utf-8 -*-
"""Read the plan allowance directly, so the gauge is right BEFORE anything is sent.

WHY THIS EXISTS
worker.py's `_emit_quota` carries the only number that tracks what actually ENDS a session
- but it arrives only when the CLI's rate-limit status TRANSITIONS, which is a side effect
of sending a message. Open the overlay and just sit there and the statusline has nothing to
put in that slot, so it falls back to the size of the conversation: a number that answers a
different question (claude_overlay's "how much of the allowance is left" note spells out
why those two are unrelated). The moment you most want to know whether you can start
something is before you've started it, and that is exactly the moment the CLI has told us
nothing. Today the answer lives on a settings page in a browser, which is a strange place to
keep it when the overlay is already on top of everything else.

This module fills the gap by asking the same endpoint the CLI's own /usage screen reads,
`GET /api/oauth/usage`, on a timer.

CREDENTIALS - A DELIBERATE, NARROW EXCEPTION TO authstate.py's DOCTRINE
authstate.py opens with "the overlay owns no credentials ... the overlay never sees a
token", and until now that was literally true. Polling breaks it: a bearer token is the only
way to ask, and the CLI exposes no offline command that would answer instead (there is no
`claude usage` subcommand; /usage is interactive-only). So the exception is drawn as
narrowly as it can be, and it is worth stating what it does NOT permit:

  * the token is re-read from the CLI's own file on every poll, kept in a local, handed to
    one request and dropped. It is never stored on an object, never persisted, never logged
    - the debug line carries a percentage and a window name, nothing else;
  * it is sent to api.anthropic.com and nowhere else. The host is a module constant, not
    configuration, so no settings file or env var can redirect a token somewhere new;
  * the request is a GET. Nothing here can spend allowance, change an account setting, or
    put a message on the wire;
  * nothing is ever refreshed. An expired token is skipped, not renewed. Token lifecycle
    stays entirely the CLI's business - a refresh race is precisely how a stored login gets
    poisoned (see authstate's note on the CLI's compare-and-swap).

DISPLAY ONLY, ON PURPOSE
The reading produced here is deliberately weaker than the CLI's. It carries no status
string: the endpoint's severity vocabulary belongs to the server, and guessing at its values
would be putting words in its mouth - so the UI colours by the number instead, which is what
_gauge_color already does at _QUOTA_HOT. Every path that SPEAKS to the user or that can send
something - the allowance warning, the armed retry - still reads the CLI's own event and is
untouched by this file. The worst failure available here is a percentage a minute out of
date; it can never be a wrong action.

Leaf module: the stdlib, plus authstate (for the credential path, and to know when another
auth source means there is no plan allowance to report) and debuglog - both of which import
nothing but the stdlib themselves. Every function degrades to None on ANY failure - offline,
a corporate proxy, a keychain-backed login with no file at all, an endpoint that changed
shape - so a poll can never break a running overlay: the statusline simply goes on doing
what it did before this module existed.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import authstate
from debuglog import dbg

_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA = "oauth-2025-04-20"              # the OAuth beta header the CLI sends on this call
_UA = "claude-overlay"                  # honest, and accepted: the endpoint doesn't care
_TIMEOUT_S = 12
_MAX_BODY = 512 * 1024                  # the real response is ~2KB; never slurp something huge
_MAX_CRED_BYTES = 256 * 1024            # mirrors authstate's cap on the same file

POLL_S = 60                             # the endpoint's own figure is ~a minute stale (its web
                                        # page says "last updated 1 minute ago"), so asking
                                        # faster buys nothing but requests
FAIL_S = 300                            # after a poll that produced nothing. Covers the common
                                        # case of "this account has no OAuth allowance at all"
                                        # (Bedrock / Vertex / API key) without spinning on it

# The windows worth showing, in tie-break order - the same names claude_overlay's
# _QUOTA_WINDOWS knows how to label, because a window it can't name would render bare.
_WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")


def _access_token():
    """The CLI's current OAuth access token, or None when there isn't a usable one.

    None deliberately covers every "don't ask" case at once: another auth source is
    configured (Bedrock / Vertex / API key - those accounts have no plan allowance for this
    endpoint to report), the file is absent because the tokens live in an OS keychain, or the
    token on disk has already expired.

    Expiry is checked here rather than by letting the server answer 401, because the CLI
    rewrites this file every time it refreshes: skipping this tick and re-reading the next
    one picks the new token up within a minute, for the cost of one stat().
    """
    if authstate.alt_auth_configured():
        return None
    try:
        p = authstate.credentials_path()
        if p.stat().st_size > _MAX_CRED_BYTES:
            return None
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            oauth = json.load(fh).get("claudeAiOauth")
        tok = oauth.get("accessToken")
        exp = oauth.get("expiresAt")              # ms since epoch, as the CLI writes it
        if not isinstance(tok, str) or not tok:
            return None
        if isinstance(exp, (int, float)) and exp > 0 and exp / 1000.0 <= time.time():
            return None
        return tok
    except Exception:
        return None


def _fetch(token, timeout=_TIMEOUT_S):
    """The endpoint's raw JSON as a dict, or None on any failure.

    An HTTPError is logged by CODE only. The body of a failed auth call is the one place a
    server might echo something about the request back, and this module's whole safety story
    is that the token never reaches disk - so it doesn't get to reach the log either.
    """
    req = urllib.request.Request(_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": _BETA,
        "Accept": "application/json",
        "User-Agent": _UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(_MAX_BODY)
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as e:
        dbg("usage", "http %s" % getattr(e, "code", "?"))
        return None
    except Exception as e:
        dbg("usage", "fetch failed: %s" % type(e).__name__)
        return None


def _epoch(ts):
    """An ISO-8601 timestamp from the endpoint as epoch SECONDS, or None.

    Converted here so exactly one shape of reading ever reaches the UI: the SDK hands
    worker.py epoch seconds, and both the statusline clock and the retry scheduler assume it.
    A tz-naive stamp is read as UTC, which is what the endpoint means; a `Z` suffix is
    normalised because fromisoformat can't read it before Python 3.11 and the floor is 3.10.
    """
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        s = ts.strip()
        if s[-1] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def reading(data):
    """Every window the endpoint reported, rescaled, or None when it reported nothing usable.

    NO WINDOW IS PICKED HERE, ON PURPOSE. This function used to collapse the endpoint's five
    windows down to whichever was furthest along, on the reasoning that the statusline has a
    single slot. Two things were wrong with that.

    The first is a bug the user felt: the 5-hour window and the weekly one are not the same
    kind of number. The weekly one climbs slowly in the background; the 5-hour one can go from
    10% to spent in one afternoon, and it is the one that ends the session you are sitting in.
    Ranking them by magnitude means that while the 5-hour window is below the weekly one -
    which is exactly the state you are in when you sit down to work - the UI is showing the
    number that will not bite, and the one that will is not even on the wire. It becomes
    visible only after overtaking, so the whole early stretch of a 5-hour window went unseen.

    The second is that picking is editorial, and this module's stated position is that it does
    not editorialise: `status` stays None because the endpoint's severity vocabulary belongs to
    the server and guessing at its values would be putting words in its mouth. Choosing which
    of five windows deserves the user's attention is the same kind of guess, made on the same
    kind of authority. "Which window earns the slot" is a display policy, so it now lives with
    the display - see claude_overlay._binding_window, which holds the rule this used to apply.

    utilization is rescaled to 0-1: this endpoint reports 47.0 for 47%, the SDK reports 0.47,
    and the UI multiplies by 100. Windows are read independently, so one malformed entry costs
    only itself; a window _WINDOWS doesn't name is dropped, because the UI would render it bare.
    """
    if not isinstance(data, dict):
        return None
    out = {}
    for name in _WINDOWS:
        w = data.get(name)
        if not isinstance(w, dict):
            continue
        u = w.get("utilization")
        if isinstance(u, bool) or not isinstance(u, (int, float)):
            continue
        out[name] = {"utilization": float(u) / 100.0, "resets_at": _epoch(w.get("resets_at"))}
    return {"windows": out} if out else None


def poll_once():
    """One end-to-end reading, or None. Every failure above collapses to None right here, so
    a caller never has to know which of them happened."""
    tok = _access_token()
    if not tok:
        return None
    return reading(_fetch(tok))


class Poller:
    """A daemon thread that puts ("quota_poll", reading) on the UI queue on a timer.

    Emits readings ONLY, never a None: a failed poll must not blank a number that was right a
    minute ago. Allowance isn't spent while the request is failing, so the previous reading
    is still the best answer available - dropping it would trade a slightly stale number for
    no number, which is the state this module exists to end.

    Stopping is an Event, not a flag, so quit() doesn't have to wait out a sleep: wait()
    returns the moment it's set. The thread is a daemon besides, so even a wedged socket
    can't keep the process alive after the window closes.
    """

    def __init__(self, ui_queue, interval=POLL_S, first_delay=2.0, fail_delay=FAIL_S):
        self.ui = ui_queue
        self.interval = interval
        self.first_delay = first_delay      # short: the first reading is the point of all this
        self.fail_delay = fail_delay
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Idempotent - a second call can't leave two threads polling the same account."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="usage-poll", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        wait = self.first_delay
        while not self._stop.wait(wait):
            try:
                r = poll_once()
            except Exception:               # belt-and-braces: poll_once already swallows
                r = None
            if r:
                # The reading goes on the queue FIRST, and the log line is guarded. It used to
                # be the other way round, with the formatting bare: `r["utilization"]` outside
                # any try, above the put. When reading() stopped emitting that key the KeyError
                # killed this daemon thread on its first successful poll, so the overlay never
                # saw an allowance again and silently fell back to showing context size. A line
                # that exists to describe the work must never be able to cost the work.
                try:
                    self.ui.put(("quota_poll", r))
                except Exception:
                    pass
                try:
                    dbg("usage", "windows " + " ".join(
                        "%s=%.2f" % (k, w.get("utilization", -1))
                        for k, w in (r.get("windows") or {}).items()))
                except Exception:
                    pass
            wait = self.interval if r else self.fail_delay
