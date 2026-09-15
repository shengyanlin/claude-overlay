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
    """Context keeps this slot unconditionally. The allowance has a slot of its own beside
    it (TestAllowanceSegment) instead of competing for this one."""

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
        warning line. The allowance has its own label now, so nothing competes for this."""
        gauge._ctx_pct = 2
        gauge._handle("quota", _q(util=0.78))
        assert "context 2%" in gauge.ctx_lbl.cget("text")

    def test_the_allowance_never_crowds_it_again(self, gauge):
        gauge._ctx_pct = 88
        gauge._handle("quota", _q(util=0.30))
        text = gauge.ctx_lbl.cget("text")
        assert "context 88%" in text and "5h" not in text


class TestAllowanceSegment:
    """The allowance, back in the statusline as text after a spell as two arcs on the
    titlebar mark. Text because a number needs its window NAMED: position can label two
    fixed tracks, but it cannot say "week/opus", and an unnamed reading had to be dropped
    rather than drawn onto whichever arc was handy."""

    def test_it_names_the_window_and_the_number(self, gauge):
        gauge._handle("quota", _q(util=0.61, window="five_hour"))
        assert "5h 61%" in gauge.quota_lbl.cget("text")

    def test_a_weekly_window_says_so(self, gauge):
        gauge._handle("quota", _q(util=0.44, window="seven_day"))
        assert "week 44%" in gauge.quota_lbl.cget("text")

    def test_per_model_weekly_windows_keep_their_qualifier(self, gauge):
        """The thing two arcs could not express: which weekly allowance this is."""
        gauge._handle("quota", _q(util=0.5, window="seven_day_opus"))
        assert "week/opus 50%" in gauge.quota_lbl.cget("text")

    def test_nothing_to_report_leaves_the_segment_empty(self, gauge):
        gauge._ctx_pct = 20
        gauge._refresh_statusline()
        assert gauge.quota_lbl.cget("text") == ""

    def test_a_barely_touched_allowance_stays_quiet(self, gauge):
        """"5h 0%" is chrome that says nothing, and it would cost the row width on a narrow
        overlay — which is what pushed the version off the end the first time round."""
        gauge._handle("quota", _q(util=0.001))
        assert gauge.quota_lbl.cget("text") == ""

    def test_nothing_that_rounds_to_zero_percent_is_shown(self, gauge):
        """The rule is tested against the ROUNDED value, not a threshold constant. A
        constant is a second copy of `:.0f`'s rounding and the two drifted at once: 0.005
        cleared a `< 0.005` floor and then rendered, via round-half-even, as "5h 0%"."""
        for u in (0.004, 0.005, 0.0049):
            gauge._handle("quota", _q(util=u))
            assert gauge.quota_lbl.cget("text") == "", f"utilization {u} printed a 0% segment"

    def test_the_smallest_number_worth_showing_is_shown(self, gauge):
        gauge._handle("quota", _q(util=0.006))
        assert "5h 1%" in gauge.quota_lbl.cget("text")

    def test_an_absurd_integer_reading_does_not_raise(self, gauge):
        """An int passes the isinstance check, and for one this large math.isfinite() is not
        even safe to ASK — converting it to float raises OverflowError, and so would
        formatting it with `:.0f`. The guard has to narrow to float BEFORE asking, and the
        comparisons that follow must never convert."""
        gauge._handle("quota", _q(util=10 ** 400))
        assert "100%" in gauge.quota_lbl.cget("text")      # clamped, not crashed

    def test_a_reading_past_its_limit_is_clamped_not_printed_raw(self, gauge):
        """The arcs clamped with min(1.0, u) and the text keeps that: "5h 340%" reads as a
        bug in the overlay rather than as a fact about the account."""
        gauge._handle("quota", _q(util=3.4))
        assert "5h 100%" in gauge.quota_lbl.cget("text")

    def test_a_negative_reading_is_dropped(self, gauge):
        gauge._handle("quota", _q(util=-0.5))
        assert gauge.quota_lbl.cget("text") == ""

    def test_nan_and_infinity_are_dropped_rather_than_raising(self, gauge):
        """Both are floats and clear the isinstance check, and round() RAISES on both
        (ValueError / OverflowError) where the old `:.0f` merely printed "nan". This runs
        on the Tk thread inside the ui_q drain, so a raise here would take out the callback
        that delivers every OTHER event, not just this label. json.loads accepts NaN and
        Infinity by default, so a CLI that emits either reaches us."""
        for u in (float("nan"), float("inf"), float("-inf")):
            gauge._handle("quota", _q(util=u))
            assert gauge.quota_lbl.cget("text") == "", f"{u} was not dropped"

    def test_a_malformed_reading_is_dropped_not_printed(self, gauge):
        gauge._handle("quota", {"status": "allowed", "utilization": None})
        assert gauge.quota_lbl.cget("text") == ""

    def test_a_boolean_is_not_a_percentage(self, gauge):
        gauge._handle("quota", {"status": "allowed", "utilization": True, "window": "five_hour"})
        assert gauge.quota_lbl.cget("text") == ""

    def test_context_and_allowance_are_shown_together(self, gauge):
        """They answer different questions — how big this conversation is, versus how much
        of the plan is left — so neither is a duplicate of the other."""
        gauge._ctx_pct = 34
        gauge._handle("quota", _q(util=0.61))
        assert "context 34%" in gauge.ctx_lbl.cget("text")
        assert "5h 61%" in gauge.quota_lbl.cget("text")

    def test_reset_time_is_held_back_until_it_matters(self, gauge):
        gauge._handle("quota", _q(util=0.20))
        assert "resets" not in gauge.quota_lbl.cget("text")

    def test_reset_time_appears_once_you_need_to_plan_around_it(self, gauge):
        gauge._handle("quota", _q(util=0.80))
        assert "resets" in gauge.quota_lbl.cget("text")

    def test_the_polled_reading_wins_the_segment(self, gauge):
        """usage.py polls every minute; the CLI speaks only on transitions, so its copy can
        be hours stale — and idle hours are exactly when the number is worth a look."""
        gauge._handle("quota", _q(util=0.10, window="five_hour"))
        gauge._handle("quota_poll", {"windows": {
            "five_hour": {"utilization": 0.55, "resets_at": None},
            "seven_day": {"utilization": 0.20, "resets_at": None}}})
        assert "5h 55%" in gauge.quota_lbl.cget("text")

    def test_the_binding_window_is_the_one_shown(self, gauge):
        gauge._handle("quota_poll", {"windows": {
            "five_hour": {"utilization": 0.20, "resets_at": None},
            "seven_day": {"utilization": 0.71, "resets_at": None}}})
        assert "week 71%" in gauge.quota_lbl.cget("text")


class TestAllowanceColour:

    def test_quiet_below_the_warning_line(self, gauge):
        gauge._handle("quota", _q(util=0.30))
        assert gauge.quota_lbl.cget("fg") == co.T["muted"]

    def test_the_cli_saying_warning_turns_it_amber(self, gauge):
        gauge._handle("quota", _q(status="allowed_warning", util=0.30))
        assert gauge.quota_lbl.cget("fg") == co.T["accent"]

    def test_a_statusless_polled_reading_still_earns_amber(self, gauge):
        """A poll carries no status at all. Without a threshold of our own, a polled 80%
        would sit in muted grey right up to _QUOTA_HOT."""
        gauge._handle("quota_poll", {"windows": {
            "five_hour": {"utilization": 0.80, "resets_at": None}}})
        assert gauge.quota_lbl.cget("fg") == co.T["accent"]

    def test_red_at_the_hot_line_even_while_the_cli_says_allowed(self, gauge):
        gauge._handle("quota", _q(status="allowed", util=0.94))
        assert gauge.quota_lbl.cget("fg") == co.T["err"]

    def test_rejected_is_red_whatever_the_number(self, gauge):
        gauge._handle("quota", _q(status="rejected", util=0.10))
        assert gauge.quota_lbl.cget("fg") == co.T["err"]

    def test_the_tier_follows_the_number_on_screen_not_the_raw_reading(self, gauge):
        """0.749 renders as "75%" and goes amber, even though 0.749 < _QUOTA_WARN. That is
        the intended reading of the rule, not a rounding accident: a label showing "75%" in
        muted grey is the same defect _QUOTA_HOT's own comment names one tier up — a grey
        94% reads as nothing being wrong. Colour and number have to agree, and the number is
        the one on screen. Pinned so the half-percent isn't "corrected" back later."""
        gauge._handle("quota", _q(status="allowed", util=0.749))
        assert "75%" in gauge.quota_lbl.cget("text")
        assert gauge.quota_lbl.cget("fg") == co.T["accent"]

    def test_a_reading_that_rounds_below_the_tier_stays_quiet(self, gauge):
        gauge._handle("quota", _q(status="allowed", util=0.744))
        assert "74%" in gauge.quota_lbl.cget("text")
        assert gauge.quota_lbl.cget("fg") == co.T["muted"]

    def test_a_reading_the_text_refuses_is_not_coloured_as_spent(self, gauge):
        """Colour and text go through ONE validator. The second copy drifted at once: the
        colour check accepted any int-or-float, so True read as 100% and infinity read as
        spent, painting the label red while the text correctly printed nothing. Invisible
        while the label is empty, and a wrong answer the moment that branch shows anything."""
        for u in (True, float("inf"), float("nan"), -0.5):
            gauge._handle("quota", _q(status="allowed", util=u))
            assert gauge.quota_lbl.cget("text") == ""
            assert gauge.quota_lbl.cget("fg") == co.T["muted"], f"{u!r} was coloured"

    def test_context_does_not_follow_the_allowance(self, gauge):
        """Two numbers, two tiers, two labels. One recolouring for the other's sake is how
        a row starts lying about which figure is the problem."""
        gauge._ctx_pct = 10
        gauge._handle("quota", _q(status="rejected", util=1.0))
        assert gauge.quota_lbl.cget("fg") == co.T["err"]
        assert gauge.ctx_lbl.cget("fg") == co.T["muted"]


class TestGaugeColour:

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
