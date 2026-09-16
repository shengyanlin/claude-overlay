# -*- coding: utf-8 -*-
"""The allowance ring on the titlebar ✻ mark, and what a polled reading may and may not do.

Most of this file is restored from the version 1e3d8ed removed: the ring came back (the
user asked for it back by name), and its contract is unchanged — two fixed tracks, the
5-hour window inside and the weekly one outside, drawn as one supersampled image because
Tk's create_arc has no antialiasing on Windows.

One class did NOT come back: TestTheRowGivesUpTheQuota. This time the statusline keeps its
allowance text — it is the only place the window's NAME and reset time fit, and
test_ui_quota.py pins that segment — so the ring and the text are layers, not rivals, and
a test that the row is bare would pin the opposite of the current design."""
import time

import pytest

import claude_overlay as co
from conftest import chat_text


def polled(u=0.47, resets_at=None, window="five_hour"):
    """A poll payload in usage.reading()'s shape: every window it saw, keyed by name."""
    return {"windows": {window: {"utilization": u, "resets_at": resets_at}}}


POLLED = polled()

WINS = {"windows": {"five_hour": {"utilization": 0.25, "resets_at": None},
                    "seven_day": {"utilization": 0.60, "resets_at": None}}}


@pytest.fixture
def gauge(overlay):
    """A clean gauge: no CLI reading, no polled reading, no context percentage, so each
    test's ring and panel show only what the test itself puts in."""
    ov = overlay
    ov._quota = None
    ov._quota_polled = None
    ov._quota_said = None
    ov._ctx_pct = None
    ov._ctx_hist.clear()
    ov._retry = None
    return ov


# ── a number before the first message ───────────────────────────────────────────────

class TestPolledReadingReachesTheRing:
    """The CLI reports its rate-limit status only when that status TRANSITIONS, i.e. as a
    side effect of sending a message — so an overlay that has just opened has nothing to
    draw until usage.py's poll lands. These two pin that the poll actually reaches the
    mark; the text segment's own poll tests live in test_ui_quota.py."""

    def test_a_poll_alone_fills_an_arc(self, gauge):
        gauge._handle("quota_poll", POLLED)
        assert gauge._ring_arcs()["five_hour"][0] == pytest.approx(0.47)

    def test_it_reaches_the_mark_not_just_the_method(self, gauge):
        before = gauge._ring_photo
        gauge._handle("quota_poll", POLLED)
        assert gauge._ring_photo is not before, "the poll must trigger a repaint"


# ── the row holds still while the panel carries the detail ──────────────────────────

class TestTheRowHoldsStill:
    """The context segment is one number. It used to grow a turns figure once a burn rate
    could be read, and hovering the mark must not reflow the row under the cursor —
    the one strip of chrome that should be stable."""

    def test_the_turns_figure_stays_out_of_the_row(self, gauge):
        gauge._ctx_pct = 40.0
        gauge._ctx_hist.extend([10.0, 25.0, 40.0])
        gauge._refresh_statusline()
        assert "context 40%" in gauge.ctx_lbl.cget("text")
        assert "turn" not in gauge.ctx_lbl.cget("text")

    def test_the_row_does_not_change_under_the_cursor(self, gauge):
        gauge._ctx_pct = 24.0
        gauge._handle("quota_poll", WINS)
        resting = gauge.ctx_lbl.cget("text")
        gauge._mark_enter()
        assert gauge.ctx_lbl.cget("text") == resting


class TestTheHoverPanel:

    def test_it_appears_on_the_mark_and_leaves_with_it(self, gauge):
        gauge._handle("quota_poll", WINS)
        gauge._mark_enter()
        assert gauge._usage_panel.winfo_manager() == "place"
        gauge._mark_leave()
        assert gauge._usage_panel.winfo_manager() == ""

    def test_it_carries_both_allowance_windows(self, gauge):
        gauge._handle("quota_poll", WINS)
        text = gauge._usage_panel_text()
        assert "5h" in text and "25%" in text
        assert "week" in text and "60%" in text

    def test_it_carries_the_turns_figure_the_row_refuses(self, gauge):
        gauge._ctx_pct = 40.0
        gauge._ctx_hist.extend([10.0, 25.0, 40.0])
        assert "context" in gauge._usage_panel_text()
        assert "turn" in gauge._usage_panel_text()

    def test_a_reset_time_travels_with_its_own_window(self, gauge):
        soon = time.time() + 1800
        gauge._handle("quota_poll", polled(resets_at=soon))
        assert time.strftime("%H:%M", time.localtime(soon)) in gauge._usage_panel_text()

    def test_it_says_so_when_there_is_nothing_to_report(self, gauge):
        """A panel that opens empty reads as a broken control."""
        assert gauge._usage_panel_text().strip()

    def test_the_mark_is_actually_wired_to_the_handlers(self, gauge):
        """The behaviour above is driven through _mark_enter directly — Tk will not dispatch
        <Enter> to a widget that was never mapped — so the binding itself needs its own
        check, along with the cursor that advertises the mark answers to something."""
        assert gauge._mark.bind("<Enter>") and gauge._mark.bind("<Leave>")
        assert gauge._mark.cget("cursor") == "hand2"

    def test_it_lives_inside_the_overlay_window(self, gauge):
        """NOT a Toplevel, deliberately. The capture exclusion that keeps the overlay out of
        screen shares — and out of the screenshots we send Claude — is set on the root HWND
        and is not inherited by a new top-level window. A tooltip in its own window would be
        visible in a Teams share while the overlay itself was not, and would land inside our
        own screenshots, because capture() skips its withdraw dance whenever the exclusion
        is active. A child placed inside root inherits all of it for free."""
        assert gauge._usage_panel.winfo_toplevel() is gauge.root


# ── and it says what it is, once ─────────────────────────────────────────────────────

class TestTheRingIntroducesItself:

    def test_the_first_reading_says_what_the_ring_is(self, gauge):
        gauge._handle("quota_poll", POLLED)
        assert "allowance" in chat_text(gauge)

    def test_it_is_not_repeated_on_every_poll(self, gauge):
        gauge._handle("quota_poll", POLLED)
        after_first = chat_text(gauge)
        gauge._handle("quota_poll", polled(0.60))
        assert chat_text(gauge) == after_first

    def test_nothing_is_said_when_there_is_no_reading_to_explain(self, gauge):
        before = chat_text(gauge)
        gauge._handle("quota_poll", {"windows": {}})
        assert chat_text(gauge) == before


# ── what a poll must NOT do ──────────────────────────────────────────────────────────

class TestPollIsDisplayOnly:
    """The polled reading is fresher and so wins the display, but it must never reach
    anything that speaks to the user or that can put a message on the wire: it carries no
    status (usage.py won't invent one from the endpoint's severity vocabulary), so an
    announcement driven off it would be a guess, and a retry re-armed by it would send a
    refused message on a guess."""

    def test_a_poll_never_announces_a_status(self, gauge):
        """Even at 99%. The warning's wording is about what to do on your next message, and
        it belongs to a status the poll doesn't have.

        The ring's one-time introduction is deliberately NOT a counter-example: it names a
        piece of UI and claims nothing about severity, which is the thing this guards. It is
        stood down here so this test watches the announcement path and only that."""
        gauge._ring_explained = True
        before = chat_text(gauge)
        gauge._handle("quota_poll", polled(0.99))
        assert chat_text(gauge) == before

    def test_a_poll_does_not_consume_the_one_announcement_the_cli_gets(self, gauge):
        """_quota_said is what makes each transition speak exactly once. If a poll could
        overwrite it, the CLI's real warning would either be swallowed or repeated."""
        gauge._handle("quota", {"status": "allowed_warning", "utilization": 0.92,
                                "resets_at": None, "window": "five_hour"})
        said_once = chat_text(gauge)
        assert "allowance is spent" in said_once
        gauge._handle("quota_poll", POLLED)
        assert gauge._quota_said == "allowed_warning"
        gauge._handle("quota", {"status": "allowed_warning", "utilization": 0.93,
                                "resets_at": None, "window": "five_hour"})
        assert chat_text(gauge) == said_once          # not said twice

    def test_a_poll_cannot_release_an_armed_retry(self, gauge):
        """An armed retry puts a previously refused message on the wire, quite possibly with
        nobody at the machine. Only the CLI's own reading may decide the window reopened."""
        gauge._quota = {"status": "rejected", "utilization": 1.0,
                        "resets_at": time.time() + 3600, "window": "five_hour"}
        gauge._retry = {"at": int(time.time() + 3600), "text": "hi", "armed": True}
        gauge._handle("quota_poll", POLLED)
        assert not gauge._retry.get("ready")

    def test_the_cli_reading_still_releases_it(self, gauge):
        """The other half of the previous test: this path must keep working."""
        gauge._retry = {"at": int(time.time() + 3600), "text": "hi", "armed": True}
        gauge._handle("quota", {"status": "allowed", "utilization": 0.1,
                                "resets_at": None, "window": "five_hour"})
        assert gauge._retry is None or gauge._retry.get("ready")

    def test_an_announcement_quotes_its_own_window_s_reset_not_the_poll_s(self, gauge):
        """The gauge and an announcement can legitimately be describing different windows.
        Pairing 'your week allowance is used up' with the 5-hour window's reset time would
        tell someone they can start again in half an hour when they can't."""
        weekly_reset = time.time() + 3 * 86400
        gauge._handle("quota_poll", polled(resets_at=time.time() + 1800))
        gauge._handle("quota", {"status": "rejected", "utilization": 1.0,
                                "resets_at": weekly_reset, "window": "seven_day"})
        said = chat_text(gauge)
        assert time.strftime("%a %H:%M", time.localtime(weekly_reset)) in said
        assert "week allowance is used up" in said


# ── wiring ───────────────────────────────────────────────────────────────────────────

def test_the_overlay_builds_a_poller_and_stops_it_on_quit(overlay):
    """conftest neuters Poller.start (no thread, no network), so what's asserted here is
    that the Overlay owns one at all and that quit() tells it to stop."""
    assert isinstance(overlay._usage_poll, co.usage.Poller)
    overlay._usage_poll.stop()
    assert overlay._usage_poll._stop.is_set()


# ── the policy that used to live in usage.py ─────────────────────────────────────────

class TestBindingWindow:
    """`_binding_window` holds the rule usage.reading() used to apply before the UI ever saw
    the data: the binding constraint is the limit you reach first. It moved here because it
    is a display decision, and because collapsing to it in the data layer threw away the
    5-hour window that the ring needs to draw."""

    def test_picks_the_window_that_is_furthest_along(self):
        w = co._binding_window({"five_hour": {"utilization": 0.12},
                                "seven_day": {"utilization": 0.71}})
        assert w["window"] == "seven_day"
        assert w["utilization"] == pytest.approx(0.71)

    def test_a_tie_goes_to_five_hour(self):
        """At equal percentages the 5-hour window is the one that can end the session you
        are sitting in right now."""
        w = co._binding_window({"seven_day": {"utilization": 0.40},
                                "five_hour": {"utilization": 0.40}})
        assert w["window"] == "five_hour"

    def test_it_carries_the_window_s_own_reset_time(self):
        w = co._binding_window({"five_hour": {"utilization": 0.5, "resets_at": 1788103800.0}})
        assert w["resets_at"] == pytest.approx(1788103800.0)

    def test_nothing_usable_is_none_not_a_crash(self):
        for bad in (None, {}, {"five_hour": None}, {"five_hour": {}}):
            assert co._binding_window(bad) is None, bad


# ── the ring on the ✻ mark ───────────────────────────────────────────────────────────

class TestRingArcs:
    """What the ring is asked to draw, kept apart from the drawing. The mark is rendered as
    one supersampled image (Tk's own create_arc has no antialiasing on Windows, and a 3px
    arc on a 36px circle turns into a visible staircase), so the values and colours are
    worth pinning somewhere a test can read them without decoding a bitmap."""

    def test_each_track_carries_its_own_window(self, gauge):
        gauge._handle("quota_poll", WINS)
        arcs = gauge._ring_arcs()
        assert arcs["five_hour"][0] == pytest.approx(0.25)
        assert arcs["week"][0] == pytest.approx(0.60)

    def test_the_five_hour_track_fills_even_while_the_week_is_further_along(self, gauge):
        """The bug the ring exists to fix. The text segment's single slot shows the binding
        window, so this state — 5h below the week, i.e. exactly where you are when you sit
        down to work — names the weekly number and leaves the 5-hour one unseen: the window
        that actually ends your session is invisible for its whole early stretch."""
        gauge._handle("quota_poll", WINS)
        assert "five_hour" in gauge._ring_arcs()

    def test_a_spent_window_goes_red(self, gauge):
        gauge._handle("quota_poll", {"windows": {"five_hour": {"utilization": 0.95}}})
        assert gauge._ring_arcs()["five_hour"][1] == co.T["err"]

    def test_a_window_past_the_warning_line_goes_amber(self, gauge):
        gauge._handle("quota_poll", {"windows": {"five_hour": {"utilization": 0.80}}})
        assert gauge._ring_arcs()["five_hour"][1] == co.T["accent"]

    def test_a_quiet_window_stays_recessive(self, gauge):
        gauge._handle("quota_poll", WINS)
        assert gauge._ring_arcs()["five_hour"][1] == co.T["muted"]

    def test_the_week_track_shows_the_hottest_of_the_per_model_windows(self, gauge):
        """seven_day, seven_day_opus and seven_day_sonnet reset on the same weekly clock, so
        they are one constraint with three faces; the face nearest its limit owns the track."""
        gauge._handle("quota_poll", {"windows": {"seven_day": {"utilization": 0.20},
                                                 "seven_day_opus": {"utilization": 0.80}}})
        assert gauge._ring_arcs()["week"][0] == pytest.approx(0.80)

    def test_an_untouched_window_gets_no_arc_at_all(self, gauge):
        """A hairline at 0% would read as "something is used"."""
        gauge._handle("quota_poll", {"windows": {"five_hour": {"utilization": 0.0}}})
        assert "five_hour" not in gauge._ring_arcs()

    def test_a_cli_reading_that_names_its_window_still_fills_its_track(self, gauge):
        """An overlay whose CLI has spoken but whose poll hasn't landed yet is not blank."""
        gauge._handle("quota", {"status": "allowed", "utilization": 0.5,
                                "resets_at": None, "window": "five_hour"})
        assert gauge._ring_arcs()["five_hour"][0] == pytest.approx(0.5)

    def test_an_unplaceable_reading_fills_nothing_rather_than_guessing(self, gauge):
        """A window with no name can't be put on a track without claiming it is one of the
        two we drew. The statusline text still carries the number, so silence costs nothing."""
        gauge._handle("quota", {"status": "allowed", "utilization": 0.5,
                                "resets_at": None, "window": None})
        assert gauge._ring_arcs() == {}

    def test_the_track_is_visible_against_the_background(self, gauge):
        """T["border"] is 1.2:1 against the surface — right for a hairline between panels,
        invisible as an empty gauge, which is why the first cut of this showed nothing at
        all on a fresh overlay."""
        assert co._contrast(gauge._ring_track(), co.T["bg"]) >= 1.6


class TestPolledJunkCannotWedgeTheUI:
    """Adversarial review, round 1 (verified by hand before fixing): _binding_window did
    float(u) on RAW polled data, and it runs before any other validation — _quota_text →
    _gauge_quota → _binding_window is the first thing _refresh_statusline evaluates. A
    json-legal absurd int overflows float() on the Tk thread inside the ui_q drain, the
    exception that kills the callback delivering every other queued event. Shipped that
    way in 1.21.0; the ring's restoration is what put a test harness around the path."""

    def test_an_absurd_polled_integer_does_not_wedge_the_gauge(self, gauge):
        gauge._handle("quota_poll", {"windows": {"five_hour": {"utilization": 10 ** 1000}}})
        assert "5h 100%" in gauge.quota_lbl.cget("text"), \
            "clamped and shown, exactly like the CLI-reading path"
        assert gauge._ring_arcs()["five_hour"][0] == pytest.approx(1.0)

    def test_polled_nan_bool_and_negative_windows_are_dropped_whole(self, gauge):
        gauge._handle("quota_poll", {"windows": {
            "five_hour": {"utilization": float("nan")},
            "seven_day": {"utilization": True},
            "seven_day_opus": {"utilization": -3},
        }})
        assert gauge._ring_arcs() == {}
        assert gauge.quota_lbl.cget("text") == ""

    def test_a_junk_poll_does_not_mask_a_valid_cli_reading(self, gauge):
        """_quota_windows falls through to the CLI's own reading when every polled window
        fails validation — a poll of pure junk used to win the display with nothing."""
        gauge._handle("quota", {"status": "allowed", "utilization": 0.5,
                                "resets_at": None, "window": "five_hour"})
        gauge._handle("quota_poll", {"windows": {"five_hour": {"utilization": float("inf")}}})
        assert gauge._ring_arcs()["five_hour"][0] == pytest.approx(0.5)


class TestRingRendering:

    def test_the_mark_carries_exactly_one_image(self, gauge):
        gauge._handle("quota_poll", WINS)
        ids = gauge._mark.find_withtag("ring")
        assert len(ids) == 1
        assert gauge._mark.type(ids[0]) == "image"

    def test_the_photo_is_kept_referenced_so_tk_cannot_collect_it(self, gauge):
        """Tk holds only a weak claim on a PhotoImage; drop the Python reference and the
        mark silently goes blank. Same reason _orb_name_photo is kept on the instance."""
        gauge._handle("quota_poll", WINS)
        assert gauge._ring_photo is not None

    def test_a_repaint_replaces_the_image_instead_of_stacking(self, gauge):
        gauge._handle("quota_poll", WINS)
        gauge._handle("quota_poll", WINS)
        assert len(gauge._mark.find_withtag("ring")) == 1

    def test_an_overlay_with_no_reading_still_draws_its_empty_tracks(self, gauge):
        """The mark is always the mark; the tracks are what make a filled arc read as "this
        much of that" rather than as a stray fragment."""
        assert len(gauge._mark.find_withtag("ring")) == 1
