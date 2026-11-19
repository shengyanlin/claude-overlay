# -*- coding: utf-8 -*-
"""Unit tests for authstate.py — the read-only view of the `claude` CLI's stored login.

The whole module is one-sided by design: it may only claim "dead" on the exact marker the
CLI writes AND reads back as dead (a blank refreshToken). Most of these tests therefore
pin the NEGATIVES — every state that must NOT be called dead — because a false positive
would refuse to send messages that would have worked.

No network, no CLI, no GUI: every test points CLAUDE_CONFIG_DIR at a tmp dir (or, for the
macOS keychain cases, at a fake keychain — the real one is never read).
"""

import hashlib
import json
import threading

import pytest

import authstate


LIVE = {"accessToken": "at-live", "refreshToken": "rt-live",
        "expiresAt": 4_000_000_000_000, "refreshTokenExpiresAt": 4_100_000_000_000}
# Exactly what the CLI writes after an invalid_grant refresh failure.
DEAD = {"accessToken": "", "refreshToken": "", "expiresAt": 0,
        "refreshTokenExpiresAt": 4_100_000_000_000}


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """An isolated CLI config dir, with every alternative-auth env var cleared so this
    machine's real ANTHROPIC_API_KEY (etc.) can't make the checks stand down."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for name in authstate._ALT_AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_creds(d, oauth, wrapper="claudeAiOauth"):
    (d / ".credentials.json").write_text(json.dumps({wrapper: oauth}), encoding="utf-8")


# ── where the credentials live ────────────────────────────────────────────────

class TestPaths:

    def test_config_dir_honours_env(self, cfgdir):
        assert authstate.config_dir() == cfgdir
        assert authstate.credentials_path() == cfgdir / ".credentials.json"

    def test_config_dir_defaults_to_dot_claude(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert authstate.config_dir().name == ".claude"

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        assert authstate.config_dir().name == ".claude"


# ── dead_reason: the POSITIVE case ────────────────────────────────────────────

class TestDeadReason:

    def test_blank_refresh_token_is_dead(self, cfgdir):
        write_creds(cfgdir, DEAD)
        assert authstate.dead_reason() is not None

    def test_live_credentials_are_not_dead(self, cfgdir):
        write_creds(cfgdir, LIVE)
        assert authstate.dead_reason() is None


# ── dead_reason: every NEGATIVE that must never be called dead ────────────────

class TestDeadReasonStandsDown:

    def test_missing_file(self, cfgdir):
        # No file at all: tokens may be in an OS keychain. No evidence → not dead.
        assert authstate.dead_reason() is None

    def test_unparseable_file(self, cfgdir):
        (cfgdir / ".credentials.json").write_text("{not json", encoding="utf-8")
        assert authstate.dead_reason() is None

    def test_empty_file(self, cfgdir):
        (cfgdir / ".credentials.json").write_text("", encoding="utf-8")
        assert authstate.dead_reason() is None

    def test_no_oauth_key(self, cfgdir):
        write_creds(cfgdir, LIVE, wrapper="somethingElse")
        assert authstate.dead_reason() is None

    def test_oauth_not_a_dict(self, cfgdir):
        (cfgdir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": "nope"}), encoding="utf-8")
        assert authstate.dead_reason() is None

    def test_expired_access_token_alone_is_not_dead(self, cfgdir):
        """The normal state between refreshes. Calling this dead would block sends every
        time a token aged out — the refresh that follows usually just works."""
        write_creds(cfgdir, {**LIVE, "expiresAt": 1})
        assert authstate.dead_reason() is None

    def test_expired_refresh_token_alone_is_not_dead(self, cfgdir):
        """Not the CLI's own dead test: the server is the only authority on whether a
        refresh token is still accepted, so we don't pre-empt it."""
        write_creds(cfgdir, {**LIVE, "refreshTokenExpiresAt": 1})
        assert authstate.dead_reason() is None

    def test_missing_refresh_token_key_is_not_dead(self, cfgdir):
        oauth = {k: v for k, v in LIVE.items() if k != "refreshToken"}
        write_creds(cfgdir, oauth)
        assert authstate.dead_reason() is None

    def test_null_refresh_token_is_not_dead(self, cfgdir):
        # Only the exact "" marker counts; null is an unknown shape, not proof.
        write_creds(cfgdir, {**LIVE, "refreshToken": None})
        assert authstate.dead_reason() is None

    def test_oversized_file_is_not_read(self, cfgdir):
        write_creds(cfgdir, DEAD)
        p = cfgdir / ".credentials.json"
        p.write_text(p.read_text(encoding="utf-8") + " " * (authstate._MAX_CRED_BYTES + 10),
                     encoding="utf-8")
        assert authstate.dead_reason() is None

    @pytest.mark.parametrize("name", authstate._ALT_AUTH_ENV)
    def test_alternative_auth_env_stands_down(self, cfgdir, monkeypatch, name):
        """A blanked OAuth record says nothing when an API key / token / Bedrock / Vertex /
        gateway is what actually authenticates the CLI."""
        write_creds(cfgdir, DEAD)
        monkeypatch.setenv(name, "1")
        assert authstate.dead_reason() is None

    def test_falsey_provider_toggle_does_not_stand_down(self, cfgdir, monkeypatch):
        """CLAUDE_CODE_USE_BEDROCK=0 means "not using Bedrock" — it must not disable the
        check the way a real setting would."""
        write_creds(cfgdir, DEAD)
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "0")
        assert authstate.dead_reason() is not None

    def test_api_key_helper_in_settings_stands_down(self, cfgdir):
        write_creds(cfgdir, DEAD)
        (cfgdir / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "/bin/get-key"}), encoding="utf-8")
        assert authstate.dead_reason() is None

    def test_blank_api_key_helper_does_not_stand_down(self, cfgdir):
        write_creds(cfgdir, DEAD)
        (cfgdir / "settings.json").write_text(
            json.dumps({"apiKeyHelper": "   "}), encoding="utf-8")
        assert authstate.dead_reason() is not None

    def test_broken_settings_json_does_not_stand_down(self, cfgdir):
        write_creds(cfgdir, DEAD)
        (cfgdir / "settings.json").write_text("{broken", encoding="utf-8")
        assert authstate.dead_reason() is not None


# ── signature ─────────────────────────────────────────────────────────────────

class TestSignature:

    def test_none_when_missing(self, cfgdir):
        assert authstate.signature() is None

    def test_stable_for_an_unchanged_file(self, cfgdir):
        write_creds(cfgdir, LIVE)
        assert authstate.signature() == authstate.signature()

    def test_changes_when_the_tokens_are_rewritten(self, cfgdir):
        write_creds(cfgdir, LIVE)
        before = authstate.signature()
        p = cfgdir / ".credentials.json"
        import os
        st = p.stat()
        write_creds(cfgdir, {**LIVE, "accessToken": "at-rotated-and-longer"})
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # deterministic mtime bump
        assert authstate.signature() != before


# ── signature: the macOS keychain record ──────────────────────────────────────

@pytest.fixture
def keychain(monkeypatch, tmp_path):
    """A Mac with no credential file and a keychain whose record the test controls. The
    `security` call is replaced outright, so this never touches the real keychain."""
    record = {"blob": None, "reads": 0}

    def read():
        record["reads"] += 1
        return record["blob"]
    monkeypatch.setattr(authstate.sys, "platform", "darwin")
    monkeypatch.setattr(authstate, "_keychain_read", read)
    monkeypatch.setattr(authstate, "_kc", {"at": None, "blob": None, "busy": False})
    monkeypatch.setattr(authstate.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return record


def _blob(token):
    return json.dumps({"claudeAiOauth": {**LIVE, "accessToken": token}})


def _sig(blob):
    return ("keychain", hashlib.sha256(blob.encode("utf-8")).hexdigest())


class _ThreadThatNeverRuns:
    """Stands in for threading.Thread: the background keychain read stays pending."""
    started = 0

    def __init__(self, target=None, **kw):
        pass

    def start(self):
        type(self).started += 1


class TestKeychainSignature:

    def test_pending_first_read_is_none_unless_asked_to_wait(self, keychain, monkeypatch):
        """The worker pins its baseline right after connecting, usually before any keychain
        read has landed. Unblocked that is None — and a None baseline can't be compared
        with anything — so the baseline call waits for the record instead."""
        monkeypatch.setattr(_ThreadThatNeverRuns, "started", 0)
        monkeypatch.setattr(authstate.threading, "Thread", _ThreadThatNeverRuns)
        keychain["blob"] = _blob("at-1")
        assert authstate.signature() is None                 # read still pending...
        assert _ThreadThatNeverRuns.started == 1 and keychain["reads"] == 0
        assert authstate.signature(block=True) == _sig(keychain["blob"])   # ...waited for
        assert keychain["reads"] == 1
        assert authstate.signature() == _sig(keychain["blob"])   # and cached for the rest
        assert _ThreadThatNeverRuns.started == 1 and keychain["reads"] == 1

    def test_block_rereads_past_a_fresh_cache(self, keychain):
        """A cached record can predate the token refresh the CLI did while starting up;
        pinned as the baseline, that refresh would later look like a rotation."""
        keychain["blob"] = _blob("at-before-connect")
        before = authstate.signature(block=True)
        keychain["blob"] = _blob("at-refreshed-during-connect")
        assert authstate.signature() == before               # cache is fresh: no re-read
        assert keychain["reads"] == 1
        assert authstate.signature(block=True) == _sig(keychain["blob"]) != before
        assert keychain["reads"] == 2
        assert authstate.signature() == _sig(keychain["blob"])

    def test_block_with_nothing_in_the_keychain_is_none(self, keychain):
        assert authstate.signature(block=True) is None
        assert keychain["reads"] == 1

    def test_reads_never_overlap_so_the_newest_one_lands_last(self, keychain, monkeypatch):
        """A slow background read that started first must not finish after — and overwrite
        — the blocking read that started second: the cache would go back to the older
        record and signature() would report a rotation that never happened."""
        in_first, release_first = threading.Event(), threading.Event()
        order = []

        def read():
            order.append("start")
            if len(order) == 1:
                in_first.set()
                release_first.wait(5)
                order.append("end")
                return _blob("at-old")
            order.append("end")
            return _blob("at-new")
        monkeypatch.setattr(authstate, "_keychain_read", read)
        got = []
        slow = threading.Thread(target=authstate._keychain_refresh, daemon=True)
        slow.start()
        assert in_first.wait(5)
        waiter = threading.Thread(target=lambda: got.append(authstate.signature(block=True)),
                                  daemon=True)
        waiter.start()
        waiter.join(0.2)
        assert waiter.is_alive() and order == ["start"]      # queued behind the first read
        release_first.set()
        slow.join(5)
        waiter.join(5)
        assert not slow.is_alive() and not waiter.is_alive()
        assert order == ["start", "end", "start", "end"]
        assert got == [_sig(_blob("at-new"))]
        assert authstate.signature() == _sig(_blob("at-new"))

    def test_a_waiter_reuses_the_read_it_queued_behind(self, keychain):
        # read_oauth(block=True) only needs a record younger than the TTL; signature
        # (block=True) needs one read after it asked. Same queue, different answers.
        keychain["blob"] = _blob("at-1")
        authstate._keychain_refresh()
        assert authstate._keychain_refresh(authstate._KEYCHAIN_TTL) == keychain["blob"]
        assert keychain["reads"] == 1
        assert authstate._keychain_refresh() == keychain["blob"]
        assert keychain["reads"] == 2

    def test_the_credential_file_makes_block_irrelevant(self, keychain, tmp_path):
        # Windows/Linux shape: the file is the fingerprint, block or not, keychain unread.
        keychain["blob"] = _blob("at-1")
        (tmp_path / ".claude").mkdir()
        write_creds(tmp_path / ".claude", LIVE)
        sig = authstate.signature()
        assert sig is not None and sig[0] != "keychain"
        assert authstate.signature(block=True) == sig
        assert keychain["reads"] == 0

    def test_block_never_reads_a_keychain_off_macos(self, keychain, monkeypatch):
        keychain["blob"] = _blob("at-1")
        monkeypatch.setattr(authstate.sys, "platform", "win32")
        assert authstate.signature(block=True) is None
        assert keychain["reads"] == 0


# ── is_auth_error_text ────────────────────────────────────────────────────────

class TestIsAuthErrorText:

    # Verbatim from the CLI: the ResultMessage detail, and OAuthRefreshDeadError's message.
    @pytest.mark.parametrize("text", [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "OAuth refresh token is no longer valid; run /login to re-authenticate",
        "OAuthRefreshDeadError",
        "Please run `claude auth login` and try again",
    ])
    def test_recognises_the_clis_wording(self, text):
        assert authstate.is_auth_error_text(text) is True

    @pytest.mark.parametrize("text", [
        None, "", 0,
        "The model was overloaded (HTTP 529). Transient — the next turn retries.",
        "rate_limit_error",
        "error_max_turns",
        "Tool call failed: file not found",
    ])
    def test_ignores_everything_else(self, text):
        assert authstate.is_auth_error_text(text) is False

    def test_case_insensitive(self):
        assert authstate.is_auth_error_text("FAILED TO AUTHENTICATE: OAUTH SESSION EXPIRED")

    def test_accepts_an_exception_object(self):
        # _open_once passes the exception itself, not str(e).
        assert authstate.is_auth_error_text(
            RuntimeError("OAuth session expired and could not be refreshed"))


def test_the_keychain_is_read_by_the_systems_own_tool_not_one_found_on_path(monkeypatch):
    """What `security … -w` prints is the stored login. The app runs with the user's whole
    shell PATH in front, so the command is named by full path: a same-named program in some
    tool's bin folder must never be the one handed that request."""
    seen = []

    class R:
        returncode, stdout = 0, b'{"claudeAiOauth": {}}'

    monkeypatch.setattr(authstate.subprocess, "run", lambda args, **kw: seen.append(args) or R())
    # conftest swaps the reader out for the whole session; it keeps the real one here.
    assert authstate._real_keychain_read() == '{"claudeAiOauth": {}}'
    assert [a[:2] for a in seen] == [["/usr/bin/security", "find-generic-password"]]
