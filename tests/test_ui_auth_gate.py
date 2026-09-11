# -*- coding: utf-8 -*-
"""UI behaviour when the `claude` CLI's stored login is dead (see authstate.py).

What must hold:
  * a send is HELD BACK, and holding it back costs the user nothing — the typed text and
    every attachment are still there afterwards (the point of the feature: the failing turn
    used to swallow both);
  * the notice appears once per death, not once per keystroke, and recovery is announced;
  * the errored-turn line stops promising "your next message is unaffected", which is true
    for an overload but false for an authentication failure;
  * none of it fires when the login is fine, or when the gate is switched off.

Drives the real Overlay from conftest (FakeWorker, no CLI, no network).
"""

import pytest

import claude_overlay as co
from conftest import chat_text


LOGIN_CMD = "claude auth login"


@pytest.fixture
def dead_login(monkeypatch):
    """Make the CLI's stored login look provably dead, for the UI code under test."""
    monkeypatch.setattr(co, "AUTH_GATE", True)
    monkeypatch.setattr(co.authstate, "dead_reason",
                        lambda: "the Claude CLI cleared its stored login")


@pytest.fixture
def live_login(monkeypatch):
    monkeypatch.setattr(co, "AUTH_GATE", True)
    monkeypatch.setattr(co.authstate, "dead_reason", lambda: None)


def type_into(ov, text):
    ov._ph_out()
    ov.entry.delete("1.0", "end")
    ov.entry.insert("1.0", text)
    ov._ph_active = False


# ── the gate ──────────────────────────────────────────────────────────────────

class TestGateHoldsSend:

    def test_nothing_is_sent(self, overlay, dead_login):
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "please read this deck and summarise it")
        ov._send_or_stop()
        assert not any(c[0] == "ask" for c in ov.worker.calls)

    def test_the_typed_prompt_survives(self, overlay, dead_login):
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "please read this deck and summarise it")
        ov._send_or_stop()
        assert "please read this deck and summarise it" in ov.entry.get("1.0", "end")

    def test_attachments_survive(self, overlay, dead_login):
        ov = overlay
        ov.auto_shot = False
        ov.pending_images = ["C:/tmp/a.png", "C:/tmp/b.png"]
        type_into(ov, "what's wrong with these")
        ov._send_or_stop()
        assert ov.pending_images == ["C:/tmp/a.png", "C:/tmp/b.png"]

    def test_it_does_not_go_busy(self, overlay, dead_login):
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        assert ov.busy is False

    def test_the_notice_names_the_fix(self, overlay, dead_login):
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        assert LOGIN_CMD in chat_text(ov)

    def test_the_notice_is_not_repeated_verbatim(self, overlay, dead_login):
        """Second attempt: a short reminder, not the whole explanation again."""
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        first = chat_text(ov)
        ov._send_or_stop()
        added = chat_text(ov)[len(first):]
        assert LOGIN_CMD in added and "cleared its stored credentials" not in added

    def test_empty_input_is_not_nagged(self, overlay, dead_login):
        """Enter on an empty box with nothing attached is a no-op, dead login or not."""
        ov = overlay
        ov.auto_shot = False
        ov.pending_shot = None
        ov.pending_images = []
        ov._ph_in()
        ov._send_or_stop()
        assert LOGIN_CMD not in chat_text(ov)

    def test_auto_shot_alone_is_a_real_send(self, overlay, dead_login):
        """With auto-shot on, an empty box still sends a screenshot turn — so it must be
        held back (and no capture taken)."""
        ov = overlay
        ov.auto_shot = True
        ov._ph_in()
        ov._send_or_stop()
        assert LOGIN_CMD in chat_text(ov)
        assert not any(c[0] == "ask" for c in ov.worker.calls)


class TestGateStandsDown:

    def test_healthy_login_sends(self, overlay, live_login):
        ov = overlay
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        assert any(c[0] == "ask" for c in ov.worker.calls)

    def test_gate_disabled_still_sends(self, overlay, dead_login, monkeypatch):
        """CLAUDE_OVERLAY_AUTH_GATE=0 → the notice may appear, but nothing is blocked."""
        ov = overlay
        monkeypatch.setattr(co, "AUTH_GATE", False)
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        assert any(c[0] == "ask" for c in ov.worker.calls)

    def test_a_read_failure_never_blocks(self, overlay, monkeypatch):
        """If reading the credential state raises, we have no evidence — send anyway."""
        ov = overlay
        monkeypatch.setattr(co, "AUTH_GATE", True)
        monkeypatch.setattr(co.authstate, "dead_reason",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        ov.auto_shot = False
        type_into(ov, "hello")
        ov._send_or_stop()
        assert any(c[0] == "ask" for c in ov.worker.calls)

    def test_stop_still_works_while_dead(self, overlay, dead_login):
        """The gate must not shadow the Stop action on a turn already in flight."""
        ov = overlay
        ov._set_busy(True)
        ov._send_or_stop()
        assert any(c[0] == "interrupt" for c in ov.worker.calls)


# ── the watchdog ──────────────────────────────────────────────────────────────

class TestWatchdog:

    def test_announces_a_death_once(self, overlay, dead_login):
        ov = overlay
        ov._auth_watchdog(10_000.0)
        after_first = chat_text(ov)
        assert LOGIN_CMD in after_first
        ov._auth_checked = 0.0                    # allow another tick through the throttle
        ov._auth_watchdog(20_000.0)
        assert chat_text(ov) == after_first       # no second announcement

    def test_announces_recovery(self, overlay, dead_login, monkeypatch):
        ov = overlay
        ov._auth_watchdog(10_000.0)
        monkeypatch.setattr(co.authstate, "dead_reason", lambda: None)
        ov._auth_checked = 0.0
        ov._auth_watchdog(20_000.0)
        assert "login restored" in chat_text(ov)

    def test_recovery_rearms_the_notice(self, overlay, dead_login, monkeypatch):
        """Dead → fixed → dead again must explain itself again, not stay silent."""
        ov = overlay
        ov._auth_watchdog(10_000.0)
        monkeypatch.setattr(co.authstate, "dead_reason", lambda: None)
        ov._auth_checked = 0.0
        ov._auth_watchdog(20_000.0)
        monkeypatch.setattr(co.authstate, "dead_reason", lambda: "dead again")
        ov._auth_checked = 0.0
        mark = len(chat_text(ov))
        ov._auth_watchdog(30_000.0)
        assert "cleared its stored credentials" in chat_text(ov)[mark:]

    def test_throttled(self, overlay, dead_login):
        ov = overlay
        ov._auth_checked = 10_000.0
        ov._auth_watchdog(10_000.0 + co.AUTH_CHECK_INTERVAL / 2)
        assert LOGIN_CMD not in chat_text(ov)

    def test_healthy_login_says_nothing(self, overlay, live_login):
        ov = overlay
        before = chat_text(ov)
        ov._auth_watchdog(10_000.0)
        assert chat_text(ov) == before

    def test_survives_a_read_failure(self, overlay, monkeypatch):
        ov = overlay
        monkeypatch.setattr(co.authstate, "dead_reason",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        ov._auth_watchdog(10_000.0)              # must not raise into the pump
        assert ov._auth_dead is False


# ── the errored-turn line ─────────────────────────────────────────────────────

class TestTurnErrorWording:

    def test_auth_failure_does_not_promise_the_next_message_works(self, overlay):
        line = overlay._format_turn_error({
            "subtype": "success", "result":
            "Failed to authenticate: OAuth session expired and could not be refreshed"})
        assert "unaffected" not in line
        assert LOGIN_CMD in line

    def test_auth_failure_still_reports_the_cli_reason(self, overlay):
        line = overlay._format_turn_error({
            "subtype": None, "result":
            "Failed to authenticate: OAuth session expired and could not be refreshed"})
        assert "OAuth session expired" in line

    def test_transient_failure_keeps_the_reassurance(self, overlay):
        line = overlay._format_turn_error({
            "subtype": "overloaded_error",
            "result": "The model was overloaded (HTTP 529). Transient — the next turn retries."})
        assert "unaffected" in line
        assert LOGIN_CMD not in line

    def test_no_detail_still_works(self, overlay):
        line = overlay._format_turn_error({"subtype": "error_max_turns", "result": None})
        assert "max turns" in line and "unaffected" in line
