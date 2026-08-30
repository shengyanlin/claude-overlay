# -*- coding: utf-8 -*-
"""Unit tests for usage.py - the plan-allowance poll that fills the statusline gauge.

Nothing here touches the network or this machine's real login: every test either points
CLAUDE_CONFIG_DIR at a tmp dir or monkeypatches the fetch. Two themes get tested harder
than the happy path, because they are the module's actual promises:

  * DEGRADATION - every failure (no file, alt auth, expired token, HTTP error, junk JSON,
    an endpoint that changed shape) must come back as None rather than raise, because a
    poll runs on a timer inside a running app and must never be able to break it;
  * THE TOKEN NEVER ESCAPES - it goes in one Authorization header and nowhere else, and in
    particular it is never handed to the debug log.
"""

import json
import time

import pytest

import authstate
import usage


LIVE = {"accessToken": "at-live", "refreshToken": "rt-live",
        "expiresAt": (time.time() + 3600) * 1000}       # the CLI writes ms since epoch


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """An isolated CLI config dir, with every alternative-auth env var cleared so this
    machine's real ANTHROPIC_API_KEY (etc.) can't make the poller stand down."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for name in authstate._ALT_AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_creds(d, oauth):
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


# ── _epoch: the endpoint speaks ISO-8601, the UI clock speaks epoch seconds ──────────

class TestEpoch:

    def test_offset_stamp_is_converted(self):
        # The exact shape the endpoint returns, microseconds and all.
        assert usage._epoch("2026-08-30T15:30:00.133934+00:00") == pytest.approx(1788103800.13, abs=0.1)

    def test_military_z_is_accepted(self):
        """fromisoformat can't read a trailing Z before 3.11 and the floor is 3.10, so the
        module normalises it. Same instant either way."""
        assert usage._epoch("2026-08-30T15:30:00Z") == usage._epoch("2026-08-30T15:30:00+00:00")

    def test_naive_stamp_is_read_as_utc(self):
        assert usage._epoch("2026-08-30T15:30:00") == usage._epoch("2026-08-30T15:30:00+00:00")

    def test_numbers_pass_through_and_junk_is_none(self):
        assert usage._epoch(1788103800) == 1788103800.0
        for bad in (None, "", "   ", "not a date", [], {}, True):
            assert usage._epoch(bad) is None, bad


# ── reading(): every window, and no opinion about which one matters ─────────────

class TestReading:

    def test_every_window_is_reported_not_just_the_one_that_binds(self):
        """The old contract collapsed to the furthest-along window before the UI ever saw the
        data, so a 5-hour window sitting below the weekly one was simply not available to
        draw — and the 5-hour is the one that ends the session you are in. Which window earns
        a slot is a DISPLAY policy; this module's job is to report what the endpoint said."""
        r = usage.reading({"five_hour": {"utilization": 12.0, "resets_at": None},
                           "seven_day": {"utilization": 71.0, "resets_at": None}})
        assert set(r["windows"]) == {"five_hour", "seven_day"}
        assert r["windows"]["five_hour"]["utilization"] == pytest.approx(0.12)
        assert r["windows"]["seven_day"]["utilization"] == pytest.approx(0.71)

    def test_no_window_is_singled_out_here(self):
        """No top-level utilization/window pair any more — that pair WAS the policy. Keeping
        a copy beside `windows` would be the same number in two places, free to drift the
        first time the rule that picks it changes."""
        r = usage.reading({"five_hour": {"utilization": 12.0},
                           "seven_day": {"utilization": 71.0}})
        assert "utilization" not in r
        assert "window" not in r

    def test_utilization_is_rescaled_to_the_sdk_s_0_to_1(self):
        """The endpoint says 47.0 for 47%; the SDK says 0.47; the UI multiplies by 100. If
        this ever stopped dividing, the gauge would read 4700%."""
        r = usage.reading({"five_hour": {"utilization": 47.0}})
        assert r["windows"]["five_hour"]["utilization"] == pytest.approx(0.47)

    def test_per_model_weekly_windows_are_eligible(self):
        r = usage.reading({"five_hour": {"utilization": 3.0},
                           "seven_day_opus": {"utilization": 90.0}})
        assert set(r["windows"]) == {"five_hour", "seven_day_opus"}

    def test_status_is_never_invented(self):
        """The endpoint's severity vocabulary is the server's. usage.py refuses to guess at
        it, so nothing downstream can announce a warning off a polled reading."""
        w = usage.reading({"five_hour": {"utilization": 99.0}})["windows"]["five_hour"]
        assert "status" not in w

    def test_resets_at_is_carried_as_epoch_seconds(self):
        r = usage.reading({"five_hour": {"utilization": 5.0,
                                         "resets_at": "2026-08-30T15:30:00+00:00"}})
        assert r["windows"]["five_hour"]["resets_at"] == pytest.approx(1788103800.0)

    def test_one_unusable_window_does_not_discard_the_others(self):
        """Windows are reported independently, so junk in one is not a reason to lose a
        good reading for another."""
        r = usage.reading({"five_hour": {"utilization": "47"},
                           "seven_day": {"utilization": 30.0}})
        assert set(r["windows"]) == {"seven_day"}

    def test_unusable_shapes_are_none_not_a_crash(self):
        for bad in (None, [], "nope", {}, {"five_hour": None}, {"five_hour": {}},
                    {"five_hour": {"utilization": None}},
                    {"five_hour": {"utilization": "47"}},
                    {"seven_day_cowork": {"utilization": 80.0}}):   # a window the UI can't label
            assert usage.reading(bad) is None, bad

    def test_a_bool_utilization_is_not_a_number(self):
        """True == 1 in Python, which would quietly render as "quota 100%"."""
        assert usage.reading({"five_hour": {"utilization": True}}) is None


# ── _access_token: every "don't ask" case collapses to None ─────────────────────────

class TestAccessToken:

    def test_reads_the_cli_s_token(self, cfgdir):
        write_creds(cfgdir, LIVE)
        assert usage._access_token() == "at-live"

    def test_alt_auth_stands_down(self, cfgdir, monkeypatch):
        """Bedrock / Vertex / API-key accounts have no plan allowance for this endpoint to
        report, so the poll shouldn't even read the file."""
        write_creds(cfgdir, LIVE)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-whatever")
        assert usage._access_token() is None

    def test_expired_token_is_skipped_not_refreshed(self, cfgdir):
        """Token lifecycle stays the CLI's job. Skipping costs one stat() and picks the
        refreshed token up on the next tick."""
        write_creds(cfgdir, dict(LIVE, expiresAt=(time.time() - 60) * 1000))
        assert usage._access_token() is None

    def test_blank_and_missing_and_unreadable_are_none(self, cfgdir):
        assert usage._access_token() is None                       # no file at all (keychain)
        write_creds(cfgdir, dict(LIVE, accessToken=""))            # the CLI's dead-login marker
        assert usage._access_token() is None
        (cfgdir / ".credentials.json").write_text("{ not json", encoding="utf-8")
        assert usage._access_token() is None
        (cfgdir / ".credentials.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
        assert usage._access_token() is None

    def test_absurdly_large_file_is_not_slurped(self, cfgdir):
        (cfgdir / ".credentials.json").write_text("x" * (usage._MAX_CRED_BYTES + 1),
                                                  encoding="utf-8")
        assert usage._access_token() is None


# ── _fetch: where the token goes, and what happens when the call fails ──────────────

class _FakeResp:
    def __init__(self, body):
        self._body = body
    def read(self, n=None):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _capture_urlopen(monkeypatch, body=b'{"five_hour": {"utilization": 5.0}}'):
    seen = {}
    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["method"] = req.get_method()
        seen["timeout"] = timeout
        return _FakeResp(body)
    monkeypatch.setattr(usage.urllib.request, "urlopen", fake)
    return seen


class TestFetch:

    def test_token_goes_to_anthropic_in_one_get_header(self, monkeypatch):
        seen = _capture_urlopen(monkeypatch)
        assert usage._fetch("at-secret") == {"five_hour": {"utilization": 5.0}}
        assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
        assert seen["method"] == "GET"                     # cannot spend, change or send
        assert seen["headers"]["authorization"] == "Bearer at-secret"
        # ...and nowhere else: no other header repeats it.
        others = [v for k, v in seen["headers"].items() if k != "authorization"]
        assert not any("at-secret" in str(v) for v in others)
        assert "at-secret" not in seen["url"]

    def test_http_error_is_none_and_logs_the_code_only(self, monkeypatch):
        """A failed auth call's body is the one place a server might echo the request back.
        The module's safety story is that the token never reaches disk, so the log gets a
        status code and nothing else."""
        import urllib.error
        logged = []
        monkeypatch.setattr(usage, "dbg", lambda kind, payload=None: logged.append(str(payload)))
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(usage._URL, 401, "Unauthorized", {}, None)
        monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
        assert usage._fetch("at-secret") is None
        assert logged and "401" in logged[0]
        assert not any("at-secret" in line for line in logged)

    def test_transport_failure_is_none(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("offline")
        monkeypatch.setattr(usage.urllib.request, "urlopen", boom)
        assert usage._fetch("at-live") is None

    def test_non_dict_or_unparseable_body_is_none(self, monkeypatch):
        _capture_urlopen(monkeypatch, body=b"[1, 2, 3]")
        assert usage._fetch("at-live") is None
        _capture_urlopen(monkeypatch, body=b"<html>proxy login</html>")
        assert usage._fetch("at-live") is None


# ── poll_once: no token means no request at all ────────────────────────────────────

def test_poll_once_without_a_token_never_calls_out(monkeypatch):
    monkeypatch.setattr(usage, "_access_token", lambda: None)
    monkeypatch.setattr(usage, "_fetch", lambda *a, **k: pytest.fail("must not fetch"))
    assert usage.poll_once() is None


def test_poll_once_end_to_end(monkeypatch):
    monkeypatch.setattr(usage, "_access_token", lambda: "at-live")
    monkeypatch.setattr(usage, "_fetch", lambda tok: {"five_hour": {"utilization": 47.0}})
    assert usage.poll_once()["windows"]["five_hour"]["utilization"] == pytest.approx(0.47)


# ── Poller: what it puts on the queue, and how long it waits ───────────────────────

class _FakeStop:
    """Stands in for the stop Event so the schedule can be tested without sleeping: records
    every wait() it's asked for and ends the loop after `stop_after` of them."""

    def __init__(self, stop_after):
        self.waits = []
        self.stop_after = stop_after

    def wait(self, t):
        self.waits.append(t)
        return len(self.waits) > self.stop_after

    def set(self):
        self.stop_after = 0


def _drive(monkeypatch, results, ticks):
    """Run Poller._run for `ticks` iterations against a scripted poll_once."""
    import queue as _q
    seq = list(results)
    monkeypatch.setattr(usage, "poll_once", lambda: seq.pop(0) if seq else None)
    p = usage.Poller(_q.Queue(), interval=60, first_delay=2.0, fail_delay=300)
    p._stop = _FakeStop(ticks)
    p._run()
    return p


class TestPoller:

    def test_a_real_reading_survives_the_trip_to_the_queue(self, monkeypatch):
        """Regression: the poll loop logged r["utilization"], a key reading() stopped
        emitting when it began reporting every window. The KeyError landed OUTSIDE the
        try around poll_once and BEFORE the put, so the daemon thread died on its first
        successful poll and the overlay never saw an allowance again — it silently fell
        back to showing context. Every other Poller test scripted its own payload, so
        none of them ever put a real reading() through this loop."""
        real = usage.reading({"five_hour": {"utilization": 38.0, "resets_at": None}})
        p = _drive(monkeypatch, [real], ticks=1)
        assert p.ui.get_nowait() == ("quota_poll", real)

    def test_emits_readings_on_the_ui_queue(self, monkeypatch):
        r = usage.reading({"five_hour": {"utilization": 47.0}})
        p = _drive(monkeypatch, [r], ticks=1)
        assert p.ui.get_nowait() == ("quota_poll", r)

    def test_a_failed_poll_does_not_blank_the_last_reading(self, monkeypatch):
        """Allowance isn't spent while the request is failing, so the previous number is
        still the best one available. Emitting None would trade slightly-stale for nothing."""
        p = _drive(monkeypatch, [None, None], ticks=2)
        assert p.ui.empty()

    def test_first_reading_is_asked_for_promptly_then_paced(self, monkeypatch):
        r = {"status": None, "utilization": 0.1, "resets_at": None, "window": "five_hour"}
        p = _drive(monkeypatch, [r, r], ticks=2)
        assert p._stop.waits[0] == 2.0            # don't make the user wait a minute for it
        assert p._stop.waits[1] == 60             # then the endpoint's own refresh cadence

    def test_a_failure_backs_off(self, monkeypatch):
        """The common no-reading case is an account with no OAuth allowance at all, which
        will never start working - retrying that at 60s forever is pure noise."""
        p = _drive(monkeypatch, [None], ticks=2)
        assert p._stop.waits[1] == 300

    def test_an_exception_from_poll_once_is_survivable(self, monkeypatch):
        import queue as _q
        def boom():
            raise RuntimeError("unexpected")
        monkeypatch.setattr(usage, "poll_once", boom)
        p = usage.Poller(_q.Queue(), interval=60, first_delay=0.0, fail_delay=300)
        p._stop = _FakeStop(1)
        p._run()                                   # must not raise out of the thread
        assert p.ui.empty()

    def test_start_is_idempotent(self, monkeypatch):
        import queue as _q
        started = []
        class _FakeThread:
            def __init__(self, **kw):
                started.append(kw.get("name"))
            def start(self):
                pass
        monkeypatch.setattr(usage.threading, "Thread", lambda **kw: _FakeThread(**kw))
        p = usage.Poller(_q.Queue())
        p.start()
        p.start()
        assert started == ["usage-poll"]           # never two threads on one account

    def test_stop_ends_the_loop_without_waiting_out_the_interval(self):
        """Real Event, real loop, one tick: quit() must not block on a 60s sleep."""
        import queue as _q
        p = usage.Poller(_q.Queue(), interval=600, first_delay=600)
        p.stop()
        started = time.monotonic()
        p._run()                                   # set() already → wait() returns at once
        assert time.monotonic() - started < 1.0
