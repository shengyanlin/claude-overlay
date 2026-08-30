# -*- coding: utf-8 -*-
"""The allowance gauge — the limit that actually ends sessions.

Context percent measures how big the conversation is. The 5-hour/weekly allowance is spent
by every message and is NOT handed back when Clear or /compact shrink the conversation, so
the two numbers drift apart completely: a window sitting at 2% next to an allowance that is
nearly gone is the normal case for short, frequent questions, and it is exactly how someone
gets cut off with no warning. The CLI reports the real figure (RateLimitEvent → the worker's
"quota"); these cover what the statusline does with it, and what happens to a message that
gets refused."""
import time

import pytest

import claude_overlay as co
from conftest import chat_text


def _q(status="allowed", util=0.42, window="five_hour", resets_in=3600):
    return {"status": status, "utilization": util, "window": window,
            "resets_at": int(time.time()) + resets_in if resets_in is not None else None}


@pytest.fixture
def gauge(overlay):
    ov = overlay
    ov.auto_shot = False            # keep these text-only; capture() would grab the real screen
    ov._cancel_retry()
    ov._quota = None
    ov._quota_said = None
    ov._last_sent = None
    ov._ctx_pct = None
    ov._ctx_hist.clear()
    ov._ctx_warned = 0.0
    ov._refresh_statusline()
    return ov


class TestGaugeText:
    """The allowance moved to the ring on the mark; the row keeps context, which the ring says
    nothing about. Hovering the mark spells the allowance back out in the row it vacated."""

    def test_the_allowance_is_a_hover_away(self, gauge):
        gauge._handle("quota", _q(util=0.78))
        gauge._mark_enter()
        assert "78%" in gauge._usage_panel.cget("text")

    def test_the_window_is_named(self, gauge):
        # 78% of five hours and 78% of a week are very different situations.
        gauge._handle("quota", _q(util=0.78, window="five_hour"))
        assert "78%" in gauge._usage_panel_text().splitlines()[0]
        gauge._handle("quota", _q(util=0.78, window="seven_day"))
        assert gauge._usage_panel_text().splitlines()[0].startswith("week")

    def test_reset_time_is_a_wall_clock(self, gauge):
        gauge._handle("quota", _q(resets_in=90 * 60))
        shown = gauge._usage_panel_text()
        assert "resets " + time.strftime("%H:%M", time.localtime(time.time() + 90 * 60)) in shown

    def test_a_distant_reset_names_the_day(self, gauge):
        # A weekly window can reopen days out, where a bare "resets 09:00" reads as tomorrow
        # morning at the latest and quietly misleads by most of a week.
        gauge._handle("quota", _q(window="seven_day", resets_in=3 * 24 * 3600))
        assert time.strftime("%a", time.localtime(time.time() + 3 * 24 * 3600)) \
            in gauge._usage_panel_text()

    def test_missing_reset_time_is_simply_omitted(self, gauge):
        gauge._handle("quota", _q(resets_in=None))
        text = gauge._usage_panel_text()
        assert "%" in text and "resets" not in text

    def test_context_falls_back_when_no_event_has_arrived(self, gauge):
        # An older CLI, or a session that hasn't transitioned yet. An empty gauge would be
        # worse than the number we've always had.
        gauge._ctx_pct = 31
        gauge._refresh_statusline()
        assert "context 31%" in gauge.ctx_lbl.cget("text")

    def test_a_malformed_event_falls_back_rather_than_showing_nonsense(self, gauge):
        gauge._ctx_pct = 31
        gauge._handle("quota", {"status": "allowed", "utilization": None})
        assert "context 31%" in gauge.ctx_lbl.cget("text")

    def test_context_no_longer_has_to_earn_the_slot(self, gauge):
        """It used to be squeezed out by the allowance and let back in only above its own
        warning line. With the allowance on the ring there is nothing left to compete with."""
        gauge._ctx_pct = 2
        gauge._handle("quota", _q(util=0.78))
        assert "context 2%" in gauge.ctx_lbl.cget("text")

    def test_the_allowance_never_crowds_it_again(self, gauge):
        gauge._ctx_pct = 88
        gauge._handle("quota", _q(util=0.30))
        text = gauge.ctx_lbl.cget("text")
        assert "context 88%" in text and "quota" not in text


class TestGaugeColour:

    def test_ordinary_use_is_quiet(self, gauge):
        gauge._handle("quota", _q(util=0.30))
        assert gauge._ring_arcs()["five_hour"][1] == co.T["muted"]

    def test_the_cli_warning_goes_amber(self, gauge):
        gauge._handle("quota", _q(status="allowed_warning", util=0.80))
        assert gauge._ring_arcs()["five_hour"][1] == co.T["accent"]

    def test_rejection_goes_red(self, gauge):
        gauge._handle("quota", _q(status="rejected", util=1.0))
        assert gauge._ring_arcs()["five_hour"][1] == co.T["err"]

    def test_a_high_number_goes_red_even_if_the_cli_is_still_calm(self, gauge):
        # The colour has to agree with the digits on screen: a grey 94% reads as fine.
        gauge._handle("quota", _q(status="allowed", util=0.94))
        assert gauge._ring_arcs()["five_hour"][1] == co.T["err"]

    def test_context_keeps_its_own_tiers_in_the_fallback(self, gauge):
        gauge._ctx_pct = 90
        gauge._refresh_statusline()
        assert gauge.ctx_lbl.cget("fg") == co.T["err"]

    def test_model_and_version_never_follow_the_gauge(self, gauge):
        gauge._model = "claude-opus-5"
        gauge._ctx_pct = 90
        gauge._handle("quota", _q(status="rejected", util=1.0))
        assert gauge.ctx_lbl.cget("fg") == co.T["err"]
        assert gauge.statusline.cget("fg") == co.T["muted"]
        assert gauge.ver_lbl.cget("fg") == co.T["muted"]


class TestAnnouncements:

    @pytest.fixture(autouse=True)
    def _ring_already_introduced(self, gauge):
        """The ring names itself once, on the first reading that arrives from either source.
        That is not an announcement - it labels a piece of UI and claims nothing about status -
        so it is stood down here to leave the transcript carrying only what _announce_quota
        put there. TestTheRingIntroducesItself covers the introduction on its own."""
        gauge._ring_explained = True

    def test_warning_says_when_it_comes_back_and_what_to_do(self, gauge):
        gauge._handle("quota", _q(status="allowed_warning", util=0.82))
        text = chat_text(gauge)
        assert "82%" in text
        assert "resets" in text
        assert "smaller model" in text and "Auto-shot" in text

    def test_a_repeated_status_does_not_repeat_itself(self, gauge):
        # The CLI emits on transition, but a reconnect replays the current status.
        for _ in range(3):
            gauge._handle("quota", _q(status="allowed_warning", util=0.82))
        assert chat_text(gauge).count("allowance") == 1

    def test_each_transition_speaks_once(self, gauge):
        gauge._handle("quota", _q(status="allowed_warning", util=0.82))
        gauge._handle("quota", _q(status="rejected", util=1.0))
        assert chat_text(gauge).count("allowance") == 2
        assert "used up" in chat_text(gauge)

    def test_ordinary_status_says_nothing(self, gauge):
        gauge._handle("quota", _q(status="allowed", util=0.30))
        assert "allowance" not in chat_text(gauge)

    def test_recovering_re_arms_the_warning(self, gauge):
        # The window reopened and is being spent again — worth hearing about a second time.
        gauge._handle("quota", _q(status="allowed_warning", util=0.82))
        gauge._handle("quota", _q(status="allowed", util=0.05))
        gauge._handle("quota", _q(status="allowed_warning", util=0.81))
        assert chat_text(gauge).count("allowance") == 2


class TestRefusedDraft:

    def _send(self, ov, text):
        ov.busy = False
        ov._ph_out()
        ov.entry.delete("1.0", "end")
        ov.entry.insert("1.0", text)
        ov._ph_active = False
        ov._send_or_stop()

    def _refuse(self, ov, subtype="rate_limit_error"):
        ov._handle("result", {"is_error": True, "subtype": subtype, "result": None,
                              "stop_reason": None, "cost": None})
        ov._handle("turn_done", None)

    def test_a_refused_message_comes_back(self, gauge):
        # It never reached Claude, and it vanishes at the exact moment the user has to wait
        # hours before retrying — the worst possible time to have to remember it.
        self._send(gauge, "summarize this file for me")
        self._refuse(gauge)
        assert gauge._entry_text() == "summarize this file for me"
        assert "back in the box" in chat_text(gauge)

    def test_it_never_overwrites_what_you_have_started_typing(self, gauge):
        self._send(gauge, "the refused one")
        gauge._ph_out()
        gauge.entry.insert("1.0", "something newer")
        gauge._ph_active = False
        self._refuse(gauge)
        assert gauge._entry_text() == "something newer"

    def test_other_errors_leave_the_box_alone(self, gauge):
        # An overload retries fine on the next send; refilling the box would duplicate the
        # message the user is about to re-send by hand anyway.
        self._send(gauge, "hello")
        self._refuse(gauge, subtype="overloaded_error")
        assert gauge._entry_text() == ""

    def test_a_clean_turn_leaves_the_box_alone(self, gauge):
        self._send(gauge, "hello")
        gauge._handle("result", {"is_error": False, "subtype": "success", "result": None,
                                 "stop_reason": None, "cost": None})
        gauge._handle("turn_done", None)
        assert gauge._entry_text() == ""

    def test_clear_discards_the_kept_draft(self, gauge):
        self._send(gauge, "from the old conversation")
        gauge.reset()
        assert gauge._last_sent is None
        self._refuse(gauge)
        assert gauge._entry_text() == ""


class TestScheduledRetry:
    """Sending a refused message the moment the allowance returns.

    Opt-in on purpose: arming it puts a message on the wire hours later, possibly with nobody
    at the machine. So the tests care less about the happy path than about every way it must
    stand down — an armed schedule that fires when the user has moved on is worse than one
    that never fires at all."""

    def _send(self, ov, text):
        ov.busy = False
        ov._ph_out()
        ov.entry.delete("1.0", "end")
        ov.entry.insert("1.0", text)
        ov._ph_active = False
        ov._send_or_stop()

    def _refuse(self, ov):
        ov._handle("result", {"is_error": True, "subtype": "rate_limit_error", "result": None,
                              "stop_reason": None, "cost": None})
        ov._handle("turn_done", None)
        ov.busy = False

    def _refused_with_offer(self, ov, text="do the thing", resets_in=3600):
        ov._handle("quota", _q(status="rejected", util=1.0, resets_in=resets_in))
        self._send(ov, text)
        ov.worker.calls.clear()
        self._refuse(ov)
        return ov._retry

    def _asks(self, ov):
        return [c for c in ov.worker.calls if c[0] == "ask"]

    def test_a_refusal_offers_the_schedule(self, gauge):
        r = self._refused_with_offer(gauge)
        assert r is not None and r["armed"] is False    # offered, NOT armed
        assert gauge._retry_btn._ustate == "idle"

    def test_no_offer_without_a_reset_time(self, gauge):
        # Nothing to schedule against. The restored draft is still the important half.
        assert self._refused_with_offer(gauge, resets_in=None) is None
        assert gauge._entry_text() == "do the thing"

    def test_offering_is_not_arming(self, gauge):
        self._refused_with_offer(gauge)
        gauge._retry["at"] = 0                          # even with the time already passed
        gauge._retry_tick()
        assert self._asks(gauge) == []

    def test_clicking_arms_it(self, gauge):
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        assert gauge._retry["armed"] is True
        assert gauge._retry_btn._ustate == "armed"

    def test_the_clock_fires_it(self, gauge):
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        gauge._retry["at"] = time.time() - 1
        gauge._retry_tick()
        (_, (text, _)), = self._asks(gauge)
        assert "do the thing" in text
        assert gauge._retry is None                     # disarmed by firing

    def test_the_cli_saying_youre_allowed_again_fires_it_early(self, gauge):
        # Better evidence than a clock we only ever got a prediction of.
        self._refused_with_offer(gauge, resets_in=4 * 3600)
        gauge._retry_btn._click(None)
        gauge._handle("quota", _q(status="allowed", util=0.02))
        assert len(self._asks(gauge)) == 1

    def test_an_early_signal_survives_being_busy(self, gauge):
        # The allowance can come back mid-turn. Dropping the signal would strand the retry
        # until a reset time that may be hours out.
        self._refused_with_offer(gauge, resets_in=4 * 3600)
        gauge._retry_btn._click(None)
        gauge.busy = True
        gauge._handle("quota", _q(status="allowed", util=0.02))
        assert self._asks(gauge) == []                  # not while a turn is running
        gauge.busy = False
        gauge._retry_tick()
        assert len(self._asks(gauge)) == 1

    def test_it_stands_down_when_the_box_has_something_newer(self, gauge):
        # Firing here would shove the user's half-written message out mid-sentence.
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        gauge.entry.delete("1.0", "end")
        gauge.entry.insert("1.0", "actually, something else")
        gauge._ph_active = False
        gauge._retry["at"] = time.time() - 1
        gauge._retry_tick()
        assert self._asks(gauge) == []
        assert gauge._retry is None
        assert "stood down" in chat_text(gauge)

    def test_sending_by_hand_cancels_it(self, gauge):
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        self._send(gauge, "I got impatient")
        assert gauge._retry is None
        gauge._retry_tick()                             # a stray tick must not resurrect it
        assert len(self._asks(gauge)) == 1

    def test_clear_cancels_it(self, gauge):
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        gauge.reset()
        assert gauge._retry is None and gauge._retry_after is None

    def test_clicking_again_cancels_it(self, gauge):
        self._refused_with_offer(gauge)
        btn = gauge._retry_btn
        btn._click(None)
        btn._click(None)
        assert gauge._retry is None
        assert btn._ustate == "off"
        assert "cancelled" in chat_text(gauge).lower()

    def test_a_cancelled_button_is_inert(self, gauge):
        self._refused_with_offer(gauge)
        btn = gauge._retry_btn
        btn._click(None)
        btn._click(None)
        btn._click(None)                                # third click on a dead button
        assert gauge._retry is None

    def test_it_fires_once_and_does_not_loop(self, gauge):
        # If the retry is refused again (a clock a minute out of step with the server is
        # enough), the answer is a fresh offer to accept — never an unattended retry loop.
        self._refused_with_offer(gauge)
        gauge._retry_btn._click(None)
        gauge._retry["at"] = time.time() - 1
        gauge._retry_tick()
        assert len(self._asks(gauge)) == 1
        self._refuse(gauge)
        assert gauge._retry["armed"] is False           # offered again, not re-armed
        gauge._retry["at"] = time.time() - 1
        gauge._retry_tick()
        assert len(self._asks(gauge)) == 1

    def test_a_second_refusal_replaces_the_old_offer(self, gauge):
        self._refused_with_offer(gauge, text="first")
        first = gauge._retry_btn
        self._refused_with_offer(gauge, text="second")
        assert gauge._retry["text"] == "second"
        assert gauge._retry_btn is not first

    def test_no_duplicate_timer_chains(self, gauge):
        # Arming and an early all-clear both drive the tick; each leaving its own after()
        # chain would double the polling and race two fires against one message.
        self._refused_with_offer(gauge, resets_in=4 * 3600)
        gauge._retry_btn._click(None)
        pending = gauge._retry_after
        gauge._retry_tick()
        assert gauge._retry_after != pending
        assert gauge._retry_after is not None
