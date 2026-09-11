# -*- coding: utf-8 -*-
"""Context budget: burn rate, turns-left, and the one-per-tier warning.

A bare "context 46%" answers a question nobody asks. The number people need is whether
they can keep going, and the unit they spend is the turn — so the statusline pairs the
percentage with a slope measured over recent turns, and says something out loud while
compacting is still cheap. Running the window dry ends the session; that is the failure
this is here to prevent."""
import pytest

import claude_overlay as co
from conftest import chat_text


def _turn(ov, pct, tokens=None):
    """One finished turn: turn_done arms the sample, the usage refresh lands after it (the
    real ordering — the worker schedules it post-turn_done) and carries the reading."""
    ov._handle("turn_done", None)
    if tokens is not None:
        ov._handle("ctx_tokens", tokens)
    ov._handle("ctx", pct)


@pytest.fixture
def gauge(overlay):
    ov = overlay
    ov._ctx_hist.clear()
    ov._ctx_warned = 0.0
    ov._ctx_sample_due = False
    ov._ctx_pct = None
    ov._ctx_tokens = None
    return ov


class TestBurnRate:

    def test_no_rate_from_a_single_turn(self, gauge):
        _turn(gauge, 10)
        assert gauge._ctx_rate() is None
        assert gauge._ctx_turns_left() is None

    def test_rate_is_percent_per_turn(self, gauge):
        for p in (10, 15, 20, 25):
            _turn(gauge, p)
        assert gauge._ctx_rate() == pytest.approx(5.0)

    def test_turns_left_divides_the_headroom_by_the_rate(self, gauge):
        for p in (10, 15, 20):
            _turn(gauge, p)
        # 80 points of headroom at 5 %/turn
        assert gauge._ctx_turns_left() == 16

    def test_rate_averages_over_a_bounded_window(self, gauge):
        # An early burst must not distort the rate forever — only the recent window counts.
        for p in (0, 40, 42, 44, 46, 48, 50, 52):
            _turn(gauge, p)
        assert len(gauge._ctx_hist) == co._CTX_RATE_TURNS
        assert gauge._ctx_rate() == pytest.approx(2.0)

    def test_a_flat_conversation_reports_no_rate(self, gauge):
        # Nothing accumulating means no honest estimate of when it runs out.
        for _ in range(3):
            _turn(gauge, 30)
        assert gauge._ctx_rate() is None
        assert gauge._ctx_turns_left() is None

    def test_compaction_restarts_the_window(self, gauge):
        # A drop means something won room back. The pre-compaction slope describes a
        # conversation that no longer exists, and averaging across the cliff would read as a
        # wildly negative (then wildly wrong) rate.
        for p in (60, 70, 80):
            _turn(gauge, p)
        _turn(gauge, 12)
        assert gauge._ctx_hist == [12.0]
        assert gauge._ctx_rate() is None

    def test_clear_forgets_the_old_conversation_rate(self, gauge):
        for p in (10, 20, 30):
            _turn(gauge, p)
        gauge.reset()
        assert gauge._ctx_hist == []
        assert gauge._ctx_turns_left() is None


class TestStatusline:

    def test_percentage_shows_without_a_rate(self, gauge):
        gauge._ctx_pct = 46
        gauge._refresh_statusline()
        assert "context 46%" in gauge.ctx_lbl.cget("text")

    def test_turns_left_joins_the_percentage(self, gauge):
        for p in (10, 15, 20):
            _turn(gauge, p)
        assert "~16 turns" in gauge._usage_panel_text()   # the row holds still; the panel carries it

    def test_one_turn_left_is_singular(self, gauge):
        for p in (50, 75):
            _turn(gauge, p)
        assert "~1 turn" in gauge._usage_panel_text()
        assert "turns" not in gauge.ctx_lbl.cget("text")

    def test_gauge_colours_by_tier(self, gauge):
        seen = {}
        for pct in (10, 75, 90):
            gauge._ctx_pct = pct
            seen[pct] = gauge._ctx_color()
        assert seen[10] != seen[75] != seen[90]
        assert seen[90] == co.T["err"]

    def test_model_and_version_keep_their_own_colour(self, gauge):
        # The whole point of splitting the labels: a red context gauge must not drag the
        # model name and version red with it.
        gauge._ctx_pct = 95
        gauge._model = "claude-opus-5"
        gauge._refresh_statusline()
        assert gauge.ctx_lbl.cget("fg") == co.T["err"]
        assert gauge.statusline.cget("fg") == co.T["muted"]
        assert gauge.ver_lbl.cget("fg") == co.T["muted"]
        assert "claude-opus-5" in gauge.statusline.cget("text")

    def test_survives_a_missing_reading(self, gauge):
        gauge._ctx_pct = None
        gauge._refresh_statusline()
        assert "—" in gauge.ctx_lbl.cget("text")


class TestWarning:

    def test_warns_once_per_tier(self, gauge):
        for p in (65, 72, 74, 76):
            _turn(gauge, p)
        assert chat_text(gauge).count("Context") == 1

    def test_second_tier_speaks_again(self, gauge):
        for p in (72, 88):
            _turn(gauge, p)
        assert chat_text(gauge).count("Context") == 2

    def test_quiet_below_the_first_tier(self, gauge):
        for p in (10, 30, 50, 65):
            _turn(gauge, p)
        assert "Context" not in chat_text(gauge)

    def test_warning_carries_the_headroom_and_the_advice(self, gauge):
        for p in (40, 55, 72):
            _turn(gauge, p)
        text = chat_text(gauge)
        assert "72% full" in text
        assert "more turn" in text
        assert "Compact now" in text

    def test_advice_prices_the_compaction_when_it_can(self, gauge, monkeypatch):
        monkeypatch.setattr(co, "_compact_history", lambda: [(60000, 96.0), (280000, 167.0)])
        gauge._ctx_tokens = 120000
        assert "~" in gauge._compact_advice()

    def test_advice_stays_silent_about_seconds_with_nothing_to_go_on(self, gauge, monkeypatch):
        # No remembered runs → no honest number. Say the actionable part and skip the guess.
        monkeypatch.setattr(co, "_compact_history", lambda: [])
        gauge._ctx_tokens = 120000
        advice = gauge._compact_advice()
        assert "Compact now" in advice and "~" not in advice

    def test_advice_never_scans_transcripts_on_the_ui_thread(self, gauge, monkeypatch):
        # That scan reads a dozen multi-MB files; half a second of disk is a visible stall in
        # a window that sits on top of the user's work.
        def boom():
            raise AssertionError("_compact_samples_from_transcripts ran on the UI thread")
        monkeypatch.setattr(co, "_compact_samples_from_transcripts", boom)
        gauge._ctx_tokens = 120000
        gauge._compact_advice()

    def test_compaction_re_arms_the_warning(self, gauge):
        # Room won back and spent again is worth hearing about a second time.
        for p in (60, 72):
            _turn(gauge, p)
        _turn(gauge, 15)
        assert gauge._ctx_warned == 0.0
        for p in (40, 71):
            _turn(gauge, p)
        assert chat_text(gauge).count("Context") == 2

    def test_silent_while_a_compaction_is_already_running(self, gauge):
        # Telling someone to compact during a compaction is noise.
        gauge._compacting = True
        try:
            for p in (60, 75):
                _turn(gauge, p)
        finally:
            gauge._compacting = False
        assert "Context" not in chat_text(gauge)


class TestSampling:

    def test_usage_refreshes_outside_a_turn_are_not_samples(self, gauge):
        # _emit_usage also runs on connect and after compaction. Only a finished TURN is a
        # data point; counting the others would inflate the turn count and flatten the rate.
        gauge._handle("ctx", 5)
        gauge._handle("ctx", 5)
        assert gauge._ctx_hist == []
        _turn(gauge, 12)
        assert gauge._ctx_hist == [12.0]

    def test_one_sample_per_turn_however_many_refreshes_arrive(self, gauge):
        _turn(gauge, 20)
        gauge._handle("ctx", 21)
        gauge._handle("ctx", 22)
        assert gauge._ctx_hist == [20.0]
        assert gauge._ctx_pct == 22        # the display still tracks the latest reading

    def test_ctx_tokens_is_recorded_for_pricing(self, gauge):
        _turn(gauge, 30, tokens=94000)
        assert gauge._ctx_tokens == 94000
