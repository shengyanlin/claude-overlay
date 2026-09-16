"""Tests for the anonymous usage ping (telemetry.py) and the two Overlay methods that
drive it.

Nothing here opens a socket: `send` is exercised through a fake opener and the Overlay
tests stub the module's `ping`. The two most important tests are the dullest ones —
`test_the_committed_endpoint_is_the_one_privacy_md_names` and
`test_the_committed_default_really_does_send` — because since the endpoint was filled in
the claim in PRIVACY.md is no longer "a stock build sends nothing" but "a stock build
sends exactly this, to exactly here". Both halves have to be pinned against the real
committed config rather than a test-local copy of it, or the page and the code can drift
apart without anything going red.
"""
import os
import tempfile
from unittest import mock

import pytest

import claude_overlay as co
import config
import telemetry


@pytest.fixture(autouse=True)
def _no_dnt(monkeypatch):
    """The suite must not inherit the developer's own DO_NOT_TRACK, or every gate test
    below would pass for the wrong reason."""
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)


# ── _clean ────────────────────────────────────────────────────────────────────────

class TestClean:
    def test_keeps_the_characters_a_version_is_made_of(self):
        assert telemetry._clean("1.22.1-rc_2") == "1.22.1-rc_2"

    def test_drops_everything_else(self):
        # A space, a quote and an ampersand are the three that would change the URL's shape
        assert telemetry._clean('1.0 "x" & y=2') == "1.0xy2"

    def test_drops_non_ascii(self):
        assert telemetry._clean("1.0-繁中") == "1.0-"

    def test_truncates(self):
        assert len(telemetry._clean("a" * 500)) == telemetry._SAFE_LEN

    def test_survives_a_value_that_is_not_a_string(self):
        assert telemetry._clean(None) == "None"
        assert telemetry._clean(3.14) == "3.14"

    def test_survives_an_object_whose_str_raises(self):
        class _Boom:
            def __str__(self):
                raise RuntimeError("no")
        assert telemetry._clean(_Boom()) == ""


# ── the install id ────────────────────────────────────────────────────────────────

class TestInstallId:
    def test_new_id_is_valid_and_unique(self):
        a, b = telemetry.new_id(), telemetry.new_id()
        assert telemetry.valid_id(a) and telemetry.valid_id(b)
        assert a != b

    @pytest.mark.parametrize("bad", [
        None, "", "not-a-uuid", "Jason Lin",
        "12345678-1234-1234-1234-12345678901",      # one char short
        "12345678-1234-1234-1234-1234567890123",    # one char long
        "12345678123412341234123456789012",         # right length, no hyphens
        "1234567g-1234-1234-1234-123456789012",     # 'g' is not hex
        "12345678-1234-1234-1234-12345678901 ",     # trailing space
    ])
    def test_rejects_anything_else(self, bad):
        assert telemetry.valid_id(bad) is False

    def test_accepts_upper_case_hex(self):
        assert telemetry.valid_id(telemetry.new_id().upper()) is True

    def test_rejects_a_uuid1_which_can_carry_the_mac_address(self):
        # Same shape, same hex, but v1 encodes a timestamp and classically the MAC —
        # accepting one would put a machine-derived id on the wire under a promise that
        # the id is random. This is the case a hex-and-hyphens check cannot see.
        import uuid
        v1 = str(uuid.uuid1())
        assert len(v1) == 36                      # it really does look identical
        assert telemetry.valid_id(v1) is False

    @pytest.mark.parametrize("version", [3, 5])
    def test_rejects_other_uuid_versions(self, version):
        import uuid
        maker = uuid.uuid3 if version == 3 else uuid.uuid5
        assert telemetry.valid_id(str(maker(uuid.NAMESPACE_DNS, "x"))) is False

    def test_rejects_a_v4_shaped_id_with_a_non_rfc4122_variant(self):
        # version nibble 4, but the variant bits say "reserved, Microsoft" — not a uuid4
        assert telemetry.valid_id("12345678-1234-4234-c234-123456789012") is False


# ── the payload ───────────────────────────────────────────────────────────────────

class TestFields:
    def test_exactly_four_keys_and_no_others(self):
        # Pinned as a SET, not a count: the risk this guards is a fifth field arriving
        # quietly, and PRIVACY.md names these four by name.
        assert set(telemetry.fields("1.22.1", telemetry.new_id())) == {"v", "id", "os", "py"}

    def test_values_are_sanitised(self):
        f = telemetry.fields('1.0 "&evil"', telemetry.new_id())
        assert f["v"] == "1.0evil"

    def test_python_version_is_major_minor_only(self):
        import sys
        assert telemetry.fields("1", "x")["py"] == "%d.%d" % sys.version_info[:2]

    def test_an_unreadable_field_stays_present_and_empty(self, monkeypatch):
        monkeypatch.setattr(telemetry, "_os_build", lambda: "")
        f = telemetry.fields("1.22.1", telemetry.new_id())
        assert f["os"] == "" and "os" in f

    def test_os_build_never_raises(self, monkeypatch):
        import platform
        monkeypatch.setattr(platform, "version", lambda: (_ for _ in ()).throw(OSError("no")))
        assert telemetry._os_build() == ""


# ── the four gates ────────────────────────────────────────────────────────────────

URL = "https://example.invalid/v"


class TestState:
    def test_all_gates_passed(self):
        assert telemetry.state(True, URL, True) == (True, "on")

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", " 1 "])
    def test_do_not_track_wins_over_everything(self, monkeypatch, value):
        monkeypatch.setenv("DO_NOT_TRACK", value)
        sending, why = telemetry.state(True, URL, True)
        assert sending is False and "DO_NOT_TRACK" in why

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_a_falsy_do_not_track_does_not_block(self, monkeypatch, value):
        monkeypatch.setenv("DO_NOT_TRACK", value)
        assert telemetry.state(True, URL, True)[0] is True

    def test_disabled_by_config(self):
        sending, why = telemetry.state(False, URL, True)
        assert sending is False and "TELEMETRY is false" in why

    @pytest.mark.parametrize("url", ["", "   ", None])
    def test_no_endpoint(self, url):
        sending, why = telemetry.state(True, url, True)
        assert sending is False and "no endpoint" in why

    @pytest.mark.parametrize("url", ["http://example.invalid/v", "HTTP://example.invalid/v",
                                     "ftp://example.invalid/v", "example.invalid/v"])
    def test_plain_http_is_refused(self, url):
        sending, why = telemetry.state(True, url, True)
        assert sending is False and "https" in why

    def test_https_is_case_insensitive(self):
        assert telemetry.state(True, "HTTPS://example.invalid/v", True)[0] is True

    def test_https_with_no_host_is_not_an_endpoint(self):
        # Passes a startswith("https://") test; is not a URL.
        sending, why = telemetry.state(True, "https://", True)
        assert sending is False and "no host" in why

    def test_a_fragment_is_refused_because_it_would_eat_the_fields(self):
        # https://h/v#note + "?v=1" appends INTO the fragment, and fragments never reach
        # a server: the request would leave and the data would not arrive, with nothing
        # anywhere reporting a fault. Refused loudly instead.
        sending, why = telemetry.state(True, "https://example.invalid/v#note", True)
        assert sending is False and "fragment" in why

    def test_an_empty_fragment_is_refused_too(self):
        # "https://h/v#" splits to an EMPTY fragment — falsy, so a truthiness test lets it
        # through, while send() still appends into it and the fields still never arrive.
        sending, why = telemetry.state(True, "https://example.invalid/v#", True)
        assert sending is False and "fragment" in why

    @pytest.mark.parametrize("url", ["https://user:pw@example.invalid/v",
                                     "https://user@example.invalid/v",
                                     "https://@example.invalid/v"])     # empty userinfo
    def test_credentials_in_the_url_are_refused(self, url):
        sending, why = telemetry.state(True, url, True)
        assert sending is False and "credentials" in why

    @pytest.mark.parametrize("url", ["https://example.invalid:99999/v",
                                     "https://example.invalid:notaport/v"])
    def test_an_unusable_port_is_refused_rather_than_reported_as_on(self, url):
        # urllib would refuse these, so without the check state() says "on" and every
        # ping silently fails — a Diagnose report that lies in the most useless direction.
        sending, why = telemetry.state(True, url, True)
        assert sending is False and "port" in why

    def test_an_ordinary_explicit_port_still_works(self):
        assert telemetry.state(True, "https://example.invalid:8443/v", True)[0] is True

    def test_a_query_string_is_allowed(self):
        # ?src=… is how a self-hosted collector gets addressed, and whoever sets the URL
        # is the person receiving the data — so this one is permitted on purpose.
        assert telemetry.state(True, "https://example.invalid/v?src=selfhosted", True)[0] is True

    def test_no_recorded_decision_means_it_sends(self):
        # Opt-out, not opt-in: nobody having said anything is not a refusal.
        assert telemetry.state(True, URL, None)[0] is True

    def test_an_explicit_prior_opt_in_still_sends(self):
        # Harmless leftover from before the model flipped, or a future toggle's "on".
        assert telemetry.state(True, URL, True)[0] is True

    def test_only_a_literal_False_opts_out(self):
        sending, why = telemetry.state(True, URL, False)
        assert sending is False and "opted out" in why

    @pytest.mark.parametrize("junk", [1, "false", "no", {}, [], "true"])
    def test_a_malformed_consent_value_is_not_treated_as_an_opt_out(self, junk):
        # Only a literal False is a recorded refusal. A corrupted or hand-typed value in
        # state.json ("false" the STRING, say) must not silently block reporting for a
        # reason nobody actually chose.
        assert telemetry.state(True, URL, junk)[0] is True


# ── sending ───────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, body=b""):
        self.body = body
        self.read_sizes = []

    def read(self, n):
        self.read_sizes.append(n)
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self):
        self.calls = []
        self.response = _FakeResponse()

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, timeout, dict(req.headers)))
        return self.response


@pytest.fixture
def opener(monkeypatch):
    fake = _FakeOpener()
    monkeypatch.setattr(telemetry.urllib.request, "build_opener", lambda *a: fake)
    return fake


class TestSend:
    def test_appends_a_query_string(self, opener):
        assert telemetry.send(URL, {"v": "1.22.1", "id": "abc"}) is True
        assert opener.calls[0][0] == URL + "?v=1.22.1&id=abc"

    def test_joins_with_an_ampersand_when_the_url_already_has_a_query(self, opener):
        telemetry.send(URL + "?src=test", {"v": "1"})
        assert opener.calls[0][0] == URL + "?src=test&v=1"

    def test_uses_the_timeout_and_names_itself(self, opener):
        telemetry.send(URL, {"v": "1"})
        _url, timeout, headers = opener.calls[0]
        assert timeout == telemetry.TIMEOUT
        assert headers.get("User-agent") == "claude-overlay"

    def test_reads_a_bounded_amount_of_the_reply(self, opener):
        telemetry.send(URL, {"v": "1"})
        assert opener.response.read_sizes == [telemetry.MAX_BODY]

    def test_any_failure_is_a_False_not_a_raise(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("proxy says no")
        monkeypatch.setattr(telemetry.urllib.request, "build_opener", _boom)
        assert telemetry.send(URL, {"v": "1"}) is False

    def test_redirects_are_refused(self):
        # Returning None from redirect_request is urllib's "do not follow" — the id must
        # never be forwarded to a host that isn't the one written in config.py.
        handler = telemetry._NoRedirect()
        assert handler.redirect_request(None, None, 302, "Found", {},
                                        "https://elsewhere.invalid/") is None

    def test_the_handler_is_installed_on_the_opener(self, monkeypatch):
        seen = {}

        def _build(*handlers):
            seen["handlers"] = handlers
            return _FakeOpener()
        monkeypatch.setattr(telemetry.urllib.request, "build_opener", _build)
        telemetry.send(URL, {"v": "1"})
        assert telemetry._NoRedirect in seen["handlers"]


class TestPing:
    def test_starts_a_daemon_thread_and_returns(self, monkeypatch):
        made = {}

        class _T:
            def __init__(self, **kw):
                made.update(kw)

            def start(self):
                made["started"] = True
        monkeypatch.setattr(telemetry.threading, "Thread", _T)
        telemetry.ping(URL, {"v": "1"})
        assert made["daemon"] is True and made["started"] is True
        assert made["name"] == "telemetry-ping"

    def test_a_thread_that_cannot_start_is_swallowed(self, monkeypatch):
        def _boom(**kw):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(telemetry.threading, "Thread", _boom)
        telemetry.ping(URL, {"v": "1"})          # must not raise


# ── the Overlay side ──────────────────────────────────────────────────────────────

class TestOverlayInstallId:
    def test_minted_once_then_stable(self, overlay):
        first = overlay._install_id()
        assert telemetry.valid_id(first)
        assert overlay._install_id() == first            # read back, not re-minted
        assert co._load_state()["install_id"] == first   # and it survived to the file

    def test_a_hand_edited_id_is_replaced_not_sent(self, overlay):
        co._save_state(install_id="Jason Lin")
        fresh = overlay._install_id()
        assert telemetry.valid_id(fresh)
        assert fresh != "Jason Lin"

    def test_refuses_rather_than_inventing_one_it_cannot_store(self, overlay, monkeypatch):
        monkeypatch.setattr(telemetry, "new_id",
                            lambda: (_ for _ in ()).throw(OSError("no entropy")))
        co._save_state(install_id=None)
        assert overlay._install_id() == ""

    def test_an_id_that_did_not_persist_is_refused(self, overlay, monkeypatch):
        """_save_state swallows its failures by design. If the write silently did
        nothing, returning the fresh id would mint a NEW one every launch and report one
        user as a hundred — a miscount invisible in the data, indistinguishable from
        growth. So the id is read back, and an id that didn't persist is not an id."""
        co._save_state(install_id=None)
        monkeypatch.setattr(co, "_save_state", lambda **kw: None)     # write goes nowhere
        assert overlay._install_id() == ""

    def test_a_write_that_landed_a_different_value_is_refused_too(self, overlay, monkeypatch):
        # Landing SOMETHING is not the test — the read-back compares the stored value to
        # the one just minted, so a racing writer that clobbered it is caught as well.
        reads = iter([{}, {"install_id": telemetry.new_id()}])   # before, then after
        monkeypatch.setattr(co, "_load_state", lambda: next(reads))
        monkeypatch.setattr(co, "_save_state", lambda **kw: None)
        assert overlay._install_id() == ""


class TestOverlayPing:
    def test_the_committed_endpoint_is_the_one_privacy_md_names(self):
        """WHERE a stock build reports to, pinned against the file rather than a copy.

        Read out of a FRESH config module with no user config and no env override, so it
        cannot be satisfied by whatever this process happens to have imported. Until the
        endpoint was filled in this test asserted the URL was empty, as the tripwire for
        the day it stopped being — that day came, so the tripwire moved rather than went
        away: it now pins the exact host.

        It should still fail loudly if the endpoint ever changes, because PRIVACY.md and
        the README name this URL in prose and the collector is not open to inspection.
        A silent repoint would send every install's id somewhere the published page does
        not describe. If the change is deliberate, both documents move in the same commit.
        """
        import importlib
        import sys
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("CLAUDE_OVERLAY_TELEMETRY")}
        env["CLAUDE_OVERLAY_CONFIG"] = os.path.join(tempfile.mkdtemp(), "absent.json")
        saved, sys.modules = sys.modules.copy(), dict(sys.modules)
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                sys.modules.pop("config", None)
                fresh = importlib.import_module("config")
                assert fresh.TELEMETRY_URL == (
                    "https://claude-overlay-telemetry.paperlane.workers.dev/v")
                assert not telemetry.bad_url(fresh.TELEMETRY_URL)
        finally:
            sys.modules = saved

    def test_the_committed_default_really_does_send(self, overlay, monkeypatch):
        """The behavioural half, and the honest one: with TELEMETRY on and nothing
        recorded in state.json — a fresh install that has touched no setting — the
        committed endpoint is reached and the ping goes out. Run against
        `config.TELEMETRY_URL` rather than a literal, so it is the shipped default being
        exercised and not a test-local stand-in that could stay green after a repoint."""
        sent = []
        monkeypatch.setattr(telemetry, "ping", lambda *a: sent.append(a))
        monkeypatch.setattr(co, "TELEMETRY", True)
        monkeypatch.setattr(co, "TELEMETRY_URL", config.TELEMETRY_URL)   # not a literal
        co._save_state(telemetry_consent=None)   # STATE_FILE is shared across the whole
        overlay._ping_telemetry()                # suite — clear a leaked opt-out from
        assert len(sent) == 1                    # whatever test happened to run before this one
        assert sent[0][0] == config.TELEMETRY_URL

    def test_clearing_the_url_in_the_json_config_is_honoured(self):
        """"" is a typo for every other string setting and a value for this one. Since
        the shipped default is now live, a user who writes {"TELEMETRY_URL": ""} to turn
        reporting off must actually turn it off — the generic _v_str would have rejected
        it, kept the live endpoint, and filed a warning nobody reads."""
        assert config._v_telemetry_url("") == ""
        assert config._v_telemetry_url("   ") == ""
        assert config._v_telemetry_url(None) is config._BAD
        off, _ = telemetry.state(True, config._v_telemetry_url(""), None)
        assert off is False

    def test_sends_by_default_with_no_recorded_decision(self, overlay, monkeypatch):
        """This IS the opt-out model end to end: a fresh install that has never touched
        any setting, with a real endpoint configured, sends — nobody has to say yes."""
        sent = []
        monkeypatch.setattr(telemetry, "ping", lambda *a: sent.append(a))
        monkeypatch.setattr(co, "TELEMETRY", True)
        monkeypatch.setattr(co, "TELEMETRY_URL", URL)
        co._save_state(telemetry_consent=None)   # see note above
        overlay._ping_telemetry()
        assert len(sent) == 1
        url, payload = sent[0]
        assert url == URL
        assert set(payload) == {"v", "id", "os", "py"}
        assert payload["v"] == co.__version__
        assert telemetry.valid_id(payload["id"])

    def test_opting_out_stops_it(self, overlay, monkeypatch):
        sent = []
        monkeypatch.setattr(telemetry, "ping", lambda *a: sent.append(a))
        monkeypatch.setattr(co, "TELEMETRY", True)
        monkeypatch.setattr(co, "TELEMETRY_URL", URL)
        co._save_state(telemetry_consent=False)
        overlay._ping_telemetry()
        assert sent == []

    def test_the_opt_out_is_re_read_every_launch(self, overlay, monkeypatch):
        # Turning it off has to take effect on the next start, not the next reinstall.
        sent = []
        monkeypatch.setattr(telemetry, "ping", lambda *a: sent.append(a))
        monkeypatch.setattr(co, "TELEMETRY", True)
        monkeypatch.setattr(co, "TELEMETRY_URL", URL)
        co._save_state(telemetry_consent=None)          # see note above
        overlay._ping_telemetry()                       # nothing recorded yet -> sends
        co._save_state(telemetry_consent=False)
        overlay._ping_telemetry()                        # recorded now -> stops
        assert len(sent) == 1

    def test_no_id_means_no_ping(self, overlay, monkeypatch):
        sent = []
        monkeypatch.setattr(telemetry, "ping", lambda *a: sent.append(a))
        monkeypatch.setattr(co, "TELEMETRY", True)
        monkeypatch.setattr(co, "TELEMETRY_URL", URL)
        co._save_state(telemetry_consent=None)          # see note above
        monkeypatch.setattr(overlay, "_install_id", lambda: "")
        overlay._ping_telemetry()
        assert sent == []

    def test_never_raises_into_the_tk_loop(self, overlay, monkeypatch):
        # It runs off root.after, so anything that escapes here lands on the UI thread.
        monkeypatch.setattr(telemetry, "state",
                            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        overlay._ping_telemetry()
