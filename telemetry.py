# -*- coding: utf-8 -*-
"""Anonymous usage reporting: what is sent, and how.

An app that screenshots your desktop and runs an agent with `bypassPermissions` has
exactly one thing going for it on this subject — that you can read all of it — so this
is stdlib-only, about a hundred lines, and imports nothing from the project.

THE AUDIT BOUNDARY, stated precisely rather than generously. This module owns what a
report contains and how it travels. It does NOT own the whole feature, and reading only
this file would leave you three answers short:

    telemetry.py         this file: the payload, the gates, the request
    claude_overlay.py    `Overlay._ping_telemetry` (when a report is attempted) and
                         `Overlay._install_id` (minting the id and storing it in
                         STATE_FILE)
    config.py            `TELEMETRY` and `TELEMETRY_URL` — whether there is an endpoint
                         at all, and which one
    preflight.py         `telemetry_line`, which prints the resulting state in a
                         Diagnose report

An earlier version of this docstring called this file "the whole of it". It is not, and
an inviting-sounding audit boundary that is wrong is worse than a dull one that is
right.

WHAT IS SENT: at most one HTTP GET per launch, carrying four query parameters:

    v    the overlay version      e.g. 1.22.1
    id   a random install id      uuid4, minted on first use, stored on this machine
    os   the OS build             e.g. 10.0.26100
    py   the Python version       major.minor only, e.g. 3.14

`fields()` below builds exactly those four and nothing else adds to one. Two things the
app contributes that are NOT in that table, named here so the list is honest: a fixed
`User-Agent: claude-overlay` header (chosen over urllib's default, which would announce
the Python version to every proxy in the path), and whatever query a self-hosted
TELEMETRY_URL carries of its own. A GET with a query string rather than a POST body is
deliberate: you can paste the URL into a browser and see precisely what leaves.

"At most one" per launch, not "one": the report is attempted 1.8s after the window
appears, so a launch shorter than that sends nothing, and the sending thread is a daemon
that dies with the process if you quit mid-request. Both are fine — a lost report costs
one row — but they are two of the reasons the resulting counts are a floor.

WHAT THIS PATH NEVER READS: anything you type, anything the model replies, screenshots,
file names or paths, your username, your hostname, your working directory, which model
you use, or anything the agent read. Note the careful wording. The APP obviously handles
your prompts and your screen — that is what it is for — and this module cannot make any
promise about the rest of the app. What it can say is that none of it is reachable from
here: `fields()` takes a version and an id, and there is no code path in this file that
touches anything else. The payload is four scalars rather than a nested object precisely
so that claim stays checkable at a glance.

WHAT IS NOT PROMISED: your IP address reaches the server the moment the connection
opens. That is how TCP works and no amount of code here changes it. Not storing it is
the *intended policy* of the endpoint, which is a statement about a server rather than
anything this file can enforce — and a hosting provider sits in the path regardless. The
guarantees that live here are the ones about what is built and sent; everything past the
request arriving is somebody's undertaking, not this module's. An over-claim in a
privacy note is worse than no claim at all, so the two are kept apart (see PRIVACY.md,
"What is not promised").

OPT-OUT, NOT OPT-IN — no first-run dialog asks first. `state()` lists the gates that
turn it off, in order: DO_NOT_TRACK in the environment, TELEMETRY=false, no endpoint
configured, an unusable endpoint, and an explicit prior refusal recorded in
STATE_FILE. On a stock build, gate three fails: TELEMETRY_URL ships empty, so nothing
is sent until a release configures one — see PRIVACY.md for what that release looks
like and how to opt out of it in advance.
"""

import os
import sys
import threading
import urllib.parse
import urllib.request

TIMEOUT = 6.0            # matches the update check: long enough for a slow proxy, short
                         # enough that a black-holed endpoint never becomes a hang
MAX_BODY = 4096          # the endpoint answers 204; read a token amount so the socket
                         # closes cleanly, and cap it so a hostile reply can't stream at us
_SAFE_LEN = 40           # per-value ceiling before it goes into the URL
_ID_LEN = 36             # canonical uuid4 text length ("8-4-4-4-12")


def _clean(value, limit=_SAFE_LEN):
    """Reduce a value to the characters a version/build string is made of, and truncate.

    Every field below comes from the environment in some sense — `platform.version()`
    reads the OS, the version string is read out of a file that a user can edit — so
    none of them are trusted into a URL as-is. Anything outside the set is dropped
    rather than escaped: a field that needs escaping is a field that is already wrong,
    and a silently shortened "3.14" beats a correctly-escaped surprise.
    """
    try:
        keep = [c for c in str(value) if c.isascii() and (c.isalnum() or c in "._-")]
        return "".join(keep)[:limit]
    except Exception:
        return ""


def _os_build():
    """The OS build, e.g. "10.0.26100" on Windows 11. Coarse on purpose: it answers
    "can I stop supporting Windows 10 yet" and nothing finer."""
    try:
        import platform
        return _clean(platform.version())
    except Exception:
        return ""


def _py_version():
    """major.minor — "3.14", not "3.14.4". The patch level has never decided a support
    question here, and every digit that doesn't earn its place is entropy for nothing."""
    try:
        return _clean("%d.%d" % sys.version_info[:2])
    except Exception:                                          # pragma: no cover - defensive
        return ""


def valid_id(install_id):
    """True for a canonical RFC 4122 **version 4** uuid, and nothing else.

    STATE_FILE is a plain JSON file a user can open and edit. If somebody types their
    name in here it must not end up in a URL, so the id is checked against what this
    module issues rather than passed through as an opaque string.

    The version check is not pedantry. A uuid**1** has the same shape and would sail
    through a hex-and-hyphens test, but it encodes a timestamp and, classically, the
    MAC address — so accepting one would put a machine-derived identifier on the wire
    under a promise that the id is random. Checking the version is the difference
    between the documented claim and a claim about punctuation.

    What this can never establish is that a hand-placed id was randomly generated:
    shape proves format, not entropy. Hence the wording in PRIVACY.md — the ids the app
    *generates* are uuid4; this function's job is to refuse everything that clearly
    isn't one.
    """
    s = str(install_id or "")
    if len(s) != _ID_LEN:                      # rejects the forms uuid.UUID() also takes:
        return False                           # no hyphens, {braces}, "urn:uuid:" prefix
    parts = s.split("-")
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    if not all(c in "0123456789abcdefABCDEF" for p in parts for c in p):
        return False
    try:
        import uuid
        parsed = uuid.UUID(s)
        return parsed.version == 4 and parsed.variant == uuid.RFC_4122
    except Exception:
        return False


def new_id():
    """A fresh install id. uuid4 = random, NOT derived from the MAC address, the
    hostname, the username or anything else about the machine — so it identifies a
    copy of the app and cannot be traced back to a person or reconstructed by anyone
    who knows the machine. Deleting it (or the whole state file) mints a new one, which
    reads as a new install; that is the intended escape hatch."""
    import uuid
    return str(uuid.uuid4())


def fields(version, install_id):
    """The complete payload. Four keys, all scalars, all sanitised.

    Ordered the way the docstring above lists them so a reader comparing the two never
    has to hunt. Empty values are kept rather than dropped: an `os=` that came back
    blank is a fact about the reading, and dropping the key would make it look like a
    version of the app that never sent one.
    """
    return {
        "v": _clean(version),
        "id": _clean(install_id, _ID_LEN),
        "os": _os_build(),
        "py": _py_version(),
    }


def bad_url(url):
    """Why this endpoint can't be used, or "" if it can.

    Split rather than string-matched, because three of these read as working and then
    don't:

      * a FRAGMENT silently eats the ping. `https://host/v#note` + `?v=1` appends into
        the fragment, and fragments are never sent to a server — the request leaves,
        the fields don't arrive, and nothing anywhere reports a problem.
      * USERINFO (`https://user:pass@host/`) puts credentials in a URL urllib will
        actually authenticate with, in a setting that needs none.
      * `https://` with no host passes a `startswith("https://")` test and is not an
        endpoint.

    A query string IS allowed: `?src=selfhosted` is how a collector gets addressed, and
    whoever sets TELEMETRY_URL is by definition the person receiving the data. That is
    why PRIVACY.md is careful to say four values are what the APP adds, rather than
    claiming to know everything in the final URL.
    """
    try:
        raw = str(url or "").strip()
        # Tested on the RAW string, not on parts.fragment: `https://host/v#` splits to an
        # EMPTY fragment, which is falsy and would sail through a truthiness test — while
        # send() still appends into it and the fields still never arrive. The delimiter is
        # what breaks the URL, so the delimiter is what's checked.
        if "#" in raw:
            return "TELEMETRY_URL has a # fragment (the fields would never be sent)"
        parts = urllib.parse.urlsplit(raw)
        if parts.scheme.lower() != "https":
            # Plain http would put the install id on the wire in clear text for every
            # box between here and the endpoint. There is no build of this that is
            # worth that, so it is refused rather than warned about.
            return "TELEMETRY_URL is not https"
        if not parts.hostname:
            return "TELEMETRY_URL has no host"
        # `is not None`, not truthiness: `https://@host/v` parses to an empty username,
        # which is userinfo that exists and is empty, not userinfo that is absent.
        if parts.username is not None or parts.password is not None:
            return "TELEMETRY_URL carries credentials"
        try:
            parts.port          # property; raises ValueError on a bad or out-of-range one
        except ValueError:
            # Worth its own branch rather than falling into "could not be parsed": without
            # it, state() answers "on" for a URL urllib will refuse, so Diagnose reports a
            # machine as reporting when every ping silently fails.
            return "TELEMETRY_URL has an invalid port"
        return ""
    except Exception:
        return "TELEMETRY_URL could not be parsed"


def state(enabled, url, consent):
    """Whether a ping will be sent, as (sending, reason) — the reason in the user's words.

    This is OPT-OUT, documented rather than gated by a first-run dialog: once an
    endpoint is configured, a launch reports unless one of these says not to. Each gate
    returns its OWN reason rather than a shared "disabled", because "I turned it off and
    it's still happening" has several different answers and a flat string sends the
    reader to check the wrong one.

    `consent` exists for exactly one case: something (a hand-edited state.json today, a
    settings toggle if one is ever built) recorded an explicit refusal. Only a literal
    `False` counts as that. Anything else — missing, `True`, or a malformed value from a
    corrupted file — is not a recorded refusal, and stays out of the way rather than
    silently blocking reports for a reason nobody chose.
    """
    try:
        dnt = os.environ.get("DO_NOT_TRACK", "").strip().lower()
        if dnt not in ("", "0", "false", "no", "off"):
            return False, "off - DO_NOT_TRACK is set in the environment"
        if not enabled:
            return False, "off - TELEMETRY is false"
        if not str(url or "").strip():
            return False, "off - no endpoint is configured (TELEMETRY_URL is empty)"
        why = bad_url(url)
        if why:
            return False, "off - " + why
        if consent is False:
            return False, "off - opted out (telemetry_consent is false)"
        return True, "on"
    except Exception:                                          # pragma: no cover - defensive
        return False, "off - could not be determined"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects.

    Following one would forward the install id to whatever host the response names —
    which is the single thing a reader of this file is trusting it not to do. The
    endpoint is a constant in config.py; if it ever answers with a redirect, the right
    outcome is a dropped ping.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(url, payload, timeout=TIMEOUT):
    """Fire the GET. True if the endpoint answered, False for every failure.

    A False is not worth acting on and is deliberately not retried: on a managed laptop
    the likeliest cause is a proxy that will refuse the next one identically (see the
    Zscaler note in PRIVACY.md), so a retry buys nothing and spends battery. A lost
    ping costs one row. The consequence is honest and worth stating: these numbers are
    a FLOOR, never a true count.
    """
    try:
        sep = "&" if "?" in url else "?"
        full = url + sep + urllib.parse.urlencode(payload)
        req = urllib.request.Request(full, headers={"User-Agent": "claude-overlay"})
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=timeout) as r:
            r.read(MAX_BODY)
        return True
    except Exception:
        return False


def ping(url, payload):
    """Send on a daemon thread and return immediately.

    Daemon because the ping must never delay quitting: this runs at startup, and a
    user who launches and closes the window inside six seconds is owed an instant
    close, not a wait on a socket. Nothing reads the result — there is no retry and no
    UI for it, which is the whole reason it is safe to fire and forget.
    """
    try:
        threading.Thread(target=send, args=(url, payload),
                         name="telemetry-ping", daemon=True).start()
    except Exception:                                          # pragma: no cover - defensive
        pass
