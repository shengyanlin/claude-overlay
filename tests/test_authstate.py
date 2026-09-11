# -*- coding: utf-8 -*-
"""Unit tests for authstate.py — the read-only view of the `claude` CLI's stored login.

The whole module is one-sided by design: it may only claim "dead" on the exact marker the
CLI writes AND reads back as dead (a blank refreshToken). Most of these tests therefore
pin the NEGATIVES — every state that must NOT be called dead — because a false positive
would refuse to send messages that would have worked.

No network, no CLI, no GUI: every test points CLAUDE_CONFIG_DIR at a tmp dir.
"""

import json

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
