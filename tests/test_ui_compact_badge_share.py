"""UI feature tests for:
  - _format_compact_result
  - Compact state machine (_start_compact_anim / _compact_tick / _stop_compact_anim)
  - Compact ETA (_compact_quantile / _compact_predict / _compact_progress)
  - compact_now() guards
  - Task-done badge (_maybe_flag_done / _set_task_badge)
  - Screen-share toggle (toggle_screen_share / _paint_share_toggle)
  - Auto-shot toggle (toggle_auto / _paint_screen_toggle)
  - Turn-error formatting (_format_turn_error)
"""
import time

import pytest
import claude_overlay as co
from conftest import chat_text


# ── helpers ──────────────────────────────────────────────────────────────────

def _stop_anim(ov):
    """Stop the compact animation cleanly so no after-timer leaks into the next test."""
    ov._stop_compact_anim({"status": "cancelled", "meta": None, "detail": None})


# ── _format_compact_result ────────────────────────────────────────────────────

def test_format_compact_result_numbers_present(overlay):
    meta = {"pre_tokens": 43196, "post_tokens": 4970}
    result = overlay._format_compact_result(meta)
    # The method formats with thousands separators: 43,196 and 4,970
    assert "43,196" in result
    assert "4,970" in result


def test_format_compact_result_saved_percentage(overlay):
    meta = {"pre_tokens": 43196, "post_tokens": 4970}
    result = overlay._format_compact_result(meta)
    assert "saved" in result.lower()
    # saved % ≈ (1 - 4970/43196)*100 ≈ 88 %
    assert "88" in result or "89" in result


def test_format_compact_result_fallback_on_missing_meta(overlay):
    # When meta is None or missing keys, returns the fallback string
    result_none = overlay._format_compact_result(None)
    result_empty = overlay._format_compact_result({})
    assert "Compacted" in result_none
    assert "Compacted" in result_empty


# ── Compact state machine ─────────────────────────────────────────────────────

def test_start_compact_anim_sets_flags(overlay):
    overlay._start_compact_anim()
    try:
        assert overlay._compacting is True
        assert overlay._compact_line is True
        assert overlay.busy is True
    finally:
        _stop_anim(overlay)


def test_start_compact_anim_inserts_banner(overlay):
    overlay._start_compact_anim()
    try:
        overlay.root.update_idletasks()
        txt = chat_text(overlay)
        # Banner must contain either "Compact" or the sparkle character
        assert "Compact" in txt or any(c in txt for c in "✶✷✸✹✺")
    finally:
        _stop_anim(overlay)


def test_compact_tick_advances_frame(overlay):
    overlay._start_compact_anim()
    try:
        frame_before = overlay._compact_frame
        # Cancel the scheduled after so tick doesn't auto-schedule another
        if overlay._compact_anim_after is not None:
            overlay.root.after_cancel(overlay._compact_anim_after)
            overlay._compact_anim_after = None
        overlay._compact_tick()
        assert overlay._compact_frame == frame_before + 1
    finally:
        _stop_anim(overlay)


def test_compact_tick_multiple_times_no_raise(overlay):
    overlay._start_compact_anim()
    try:
        for _ in range(5):
            if overlay._compact_anim_after is not None:
                overlay.root.after_cancel(overlay._compact_anim_after)
                overlay._compact_anim_after = None
            overlay._compact_tick()  # must not raise
    finally:
        _stop_anim(overlay)


def test_stop_compact_anim_ok_clears_compacting(overlay):
    overlay._start_compact_anim()
    overlay._stop_compact_anim({"status": "ok",
                                "meta": {"pre_tokens": 43196, "post_tokens": 4970},
                                "detail": None})
    assert overlay._compacting is False
    assert overlay.busy is False


def test_stop_compact_anim_ok_result_in_chat(overlay):
    overlay._start_compact_anim()
    overlay._stop_compact_anim({"status": "ok",
                                "meta": {"pre_tokens": 43196, "post_tokens": 4970},
                                "detail": None})
    txt = chat_text(overlay)
    # Result line must mention token counts
    assert "43,196" in txt or "43196" in txt


def test_stop_compact_anim_cancelled(overlay):
    overlay._start_compact_anim()
    overlay._stop_compact_anim({"status": "cancelled", "meta": None, "detail": None})
    assert overlay._compacting is False
    assert overlay.busy is False
    txt = chat_text(overlay)
    # "cancelled" status → "Compaction stopped" line
    assert "stopped" in txt or "cancelled" in txt.lower() or "unchanged" in txt


def test_stop_compact_anim_timeout(overlay):
    overlay._start_compact_anim()
    overlay._stop_compact_anim({"status": "timeout", "meta": None, "detail": None})
    assert overlay._compacting is False
    txt = chat_text(overlay)
    assert "timed out" in txt or "timeout" in txt.lower() or "unchanged" in txt


def test_stop_compact_anim_unconfirmed(overlay):
    overlay._start_compact_anim()
    overlay._stop_compact_anim({"status": "unconfirmed", "meta": None, "detail": None})
    assert overlay._compacting is False
    txt = chat_text(overlay)
    assert "unconfirmed" in txt.lower() or "couldn" in txt or "confirm" in txt


# ── Compact ETA ───────────────────────────────────────────────────────────────

def test_compact_quantile_single_and_interpolated():
    assert co._compact_quantile([], 0.8) == 0.0
    assert co._compact_quantile([7.0], 0.8) == 7.0
    # 0.5 across [0, 10, 20] lands exactly on the middle sample; 0.75 halfway past it
    assert co._compact_quantile([0.0, 10.0, 20.0], 0.5) == 10.0
    assert co._compact_quantile([0.0, 10.0, 20.0], 0.75) == 15.0


def test_compact_predict_none_without_samples():
    assert co._compact_predict([], 50_000) is None
    assert co._compact_predict([(0, 12.0), (40_000, 0)], 50_000) is None   # junk rows dropped


def test_compact_predict_pads_above_the_fit():
    """Samples on a perfect line leave zero residual, so only the floor pad applies — but it
    must apply, because a 2-point fit is exactly where confidence is lowest."""
    pts = [(20_000, 100.0), (40_000, 200.0)]
    assert co._compact_predict(pts, 30_000) == pytest.approx(150.0 * (1 + co._COMPACT_ETA_PAD))


def test_compact_predict_pads_by_residual_spread_when_noisy():
    """A scattered sample set should buy MORE headroom than the flat floor pad."""
    pts = [(20_000, 100.0), (40_000, 120.0), (60_000, 400.0), (80_000, 180.0)]
    fit = 200.0                       # by symmetry the line through these lands on mean(y) at 50k
    assert co._compact_predict(pts, 50_000) > fit * (1 + co._COMPACT_ETA_PAD)


def test_compact_predict_median_fallback_is_padded():
    # One sample ⇒ no fit; same-size samples ⇒ no slope. Both land on the median branch.
    assert co._compact_predict([(50_000, 100.0)], 50_000) > 100.0
    assert co._compact_predict([(50_000, 100.0), (50_000, 140.0)], 50_000) > 120.0


def test_compact_predict_clamped():
    assert co._compact_predict([(50_000, 0.5)], 50_000) == co._COMPACT_ETA_MIN
    assert co._compact_predict([(50_000, 5000.0)], 50_000) == co._COMPACT_ETA_MAX


def test_compact_progress_monotonic_and_capped(overlay):
    f = overlay._compact_progress
    assert f(0, 100) == 0.0
    assert f(-5, 100) == 0.0          # clock skew must not go negative
    assert f(50, 100) == pytest.approx(0.45)
    assert f(100, 100) == pytest.approx(0.90)
    assert f(500, 100) == pytest.approx(0.90)   # never runs away past the prediction
    assert f(50, 0) == 0.0            # no ETA ⇒ nothing to be a fraction of


def test_compact_progress_never_fills_the_bar(overlay):
    """A visually full bar would claim a completion only the CLI's done event can prove."""
    for elapsed in (99, 100, 101, 400):
        assert "░" in overlay._compact_bar_filled(overlay._compact_progress(elapsed, 100))


def _tick_once(ov, eta, elapsed):
    """Render one banner frame as if `elapsed` seconds had passed against `eta`."""
    ov._start_compact_anim()
    ov._compact_eta = eta
    ov._compact_t0 = time.monotonic() - elapsed
    if ov._compact_anim_after is not None:
        ov.root.after_cancel(ov._compact_anim_after)
        ov._compact_anim_after = None
    ov._compact_tick()
    return chat_text(ov)


def test_compact_tick_shows_percentage_before_the_eta(overlay):
    try:
        txt = _tick_once(overlay, 100.0, 50.0)
        assert "45%" in txt
        assert "Compacting conversation" in txt
    finally:
        _stop_anim(overlay)


def test_compact_tick_past_eta_drops_the_percentage(overlay):
    """Past the prediction there is no honest percentage left: a bar creeping 97 → 98 % looks
    hung. Say we're still going and show the one number we actually measured."""
    try:
        txt = _tick_once(overlay, 100.0, 142.0)
        assert "%" not in txt
        assert "Still compacting" in txt
        assert "2m22s" in txt
    finally:
        _stop_anim(overlay)


def test_compact_tick_without_eta_shows_elapsed(overlay):
    try:
        txt = _tick_once(overlay, None, 30.0)
        assert "%" not in txt
        assert "30s" in txt
        # Nothing was ever predicted, so there's no expectation to have exceeded.
        assert "Still compacting" not in txt
        assert "Compacting conversation" in txt
    finally:
        _stop_anim(overlay)


# ── compact_now() guards ──────────────────────────────────────────────────────

def test_compact_now_while_busy_adds_sys_line(overlay):
    overlay.busy = True
    overlay.compact_now()
    txt = chat_text(overlay)
    # Must tell the user to finish/stop first; must NOT call worker.compact
    assert "finish" in txt.lower() or "stop" in txt.lower()
    compact_calls = [c for c in overlay.worker.calls if c[0] == "compact"]
    assert len(compact_calls) == 0
    overlay.busy = False  # restore


def test_compact_now_while_busy_no_worker_call(overlay):
    overlay.busy = True
    overlay.compact_now()
    assert not any(c[0] == "compact" for c in overlay.worker.calls)
    overlay.busy = False


def test_compact_now_while_compacting_is_noop(overlay):
    overlay._compacting = True
    overlay.compact_now()
    # No sys line added and no worker call
    assert not any(c[0] == "compact" for c in overlay.worker.calls)
    overlay._compacting = False  # restore


def test_compact_now_idle_records_worker_call(overlay):
    overlay.compact_now()
    assert any(c[0] == "compact" for c in overlay.worker.calls)
    # Clean up: cancel animation if it was started externally
    if overlay._compacting:
        _stop_anim(overlay)


# ── Task-done badge ───────────────────────────────────────────────────────────

def test_maybe_flag_done_sets_badge_when_collapsed(overlay):
    # Collapse first so the flag applies to the collapsed state
    if overlay.expanded:
        overlay.toggle_collapse()
    overlay.root.update_idletasks()
    overlay._turn_raw = "an answer"
    overlay._maybe_flag_done()
    assert overlay._task_done_badge is True
    # Restore
    overlay.toggle_collapse()


def test_badge_persists_across_expand_collapse(overlay):
    # Set the badge while collapsed
    if overlay.expanded:
        overlay.toggle_collapse()
    overlay.root.update_idletasks()
    overlay._turn_raw = "an answer"
    overlay._maybe_flag_done()
    assert overlay._task_done_badge is True
    # Expand then re-collapse: badge must survive
    overlay.toggle_collapse()   # → expanded
    overlay.root.update_idletasks()
    assert overlay._task_done_badge is True
    overlay.toggle_collapse()   # → collapsed again
    overlay.root.update_idletasks()
    assert overlay._task_done_badge is True
    # Restore
    overlay.toggle_collapse()   # back to expanded


def test_set_task_badge_false_clears(overlay):
    # Start with badge on
    if overlay.expanded:
        overlay.toggle_collapse()
    overlay.root.update_idletasks()
    overlay._turn_raw = "some reply"
    overlay._maybe_flag_done()
    assert overlay._task_done_badge is True
    overlay._set_task_badge(False)
    assert overlay._task_done_badge is False
    # Restore to expanded
    overlay.toggle_collapse()


def test_maybe_flag_done_empty_turn_does_not_set_badge(overlay):
    overlay._turn_raw = ""
    overlay._task_done_badge = False
    overlay._maybe_flag_done()
    assert overlay._task_done_badge is False


# ── Screen-share toggle ───────────────────────────────────────────────────────

def test_toggle_screen_share_flips_state(overlay):
    before = overlay.share_visible
    overlay.toggle_screen_share()
    assert overlay.share_visible is not before
    # Restore
    overlay.toggle_screen_share()


def test_toggle_screen_share_reflected_in_gear_menu(overlay):
    # Shareable moved into the ⚙ settings menu; its row shows a ✓ exactly when it's on.
    import claude_overlay as co
    overlay.share_visible = co.SHOW_IN_SCREEN_SHARE_DEFAULT
    overlay._paint_share_toggle()
    overlay.toggle_screen_share()
    overlay.root.update_idletasks()
    row = next(lbl for lbl, _ in overlay._gear_items() if "Shareable" in lbl)
    assert ("✓" in row) if overlay.share_visible else ("✓" not in row)
    # Restore
    overlay.toggle_screen_share()


def test_toggle_screen_share_adds_confirmation_line(overlay):
    overlay.toggle_screen_share()
    txt = chat_text(overlay)
    assert "share" in txt.lower() or "shareable" in txt.lower() or "private" in txt.lower()
    # Restore
    overlay.toggle_screen_share()


def test_toggle_screen_share_two_times_restores_state(overlay):
    before = overlay.share_visible
    overlay.toggle_screen_share()
    overlay.toggle_screen_share()
    assert overlay.share_visible == before


# ── Auto-shot toggle ──────────────────────────────────────────────────────────

def test_toggle_auto_flips_state(overlay):
    before = overlay.auto_shot
    overlay.toggle_auto()
    assert overlay.auto_shot is not before
    overlay.toggle_auto()  # restore


def test_toggle_auto_label_shows_auto_shot(overlay):
    overlay.toggle_auto()
    overlay.root.update_idletasks()
    txt = overlay.toggle_screen.cget("text")
    assert "Auto-shot" in txt
    overlay.toggle_auto()  # restore


def test_toggle_auto_label_prefix_on(overlay):
    overlay.auto_shot = False
    overlay._paint_screen_toggle()
    overlay.toggle_auto()  # → True
    overlay.root.update_idletasks()
    txt = overlay.toggle_screen.cget("text")
    assert "◉" in txt
    overlay.toggle_auto()  # restore


def test_toggle_auto_label_prefix_off(overlay):
    overlay.auto_shot = True
    overlay._paint_screen_toggle()
    overlay.toggle_auto()  # → False
    overlay.root.update_idletasks()
    txt = overlay.toggle_screen.cget("text")
    assert "○" in txt
    overlay.toggle_auto()  # restore


# ── Turn-error formatting ─────────────────────────────────────────────────────

def test_format_turn_error_overloaded(overlay):
    payload = {"is_error": True, "subtype": "overloaded_error",
               "result": None, "stop_reason": None}
    msg = overlay._format_turn_error(payload)
    assert "overloaded" in msg.lower()
    assert "unaffected" in msg.lower()


def test_format_turn_error_rate_limit(overlay):
    payload = {"is_error": True, "subtype": "rate_limit_error",
               "result": None, "stop_reason": None}
    msg = overlay._format_turn_error(payload)
    assert "rate" in msg.lower() or "limit" in msg.lower()
    assert "unaffected" in msg.lower()


def test_format_turn_error_max_turns(overlay):
    payload = {"is_error": True, "subtype": "error_max_turns",
               "result": None, "stop_reason": None}
    msg = overlay._format_turn_error(payload)
    assert "max" in msg.lower() and "turn" in msg.lower()
    assert "unaffected" in msg.lower()


def test_format_turn_error_execution(overlay):
    payload = {"is_error": True, "subtype": "error_during_execution",
               "result": None, "stop_reason": None}
    msg = overlay._format_turn_error(payload)
    assert "during" in msg.lower() or "execution" in msg.lower()
    assert "unaffected" in msg.lower()


def test_format_turn_error_returns_string(overlay):
    payload = {"is_error": True, "subtype": "some_unknown_error",
               "result": None, "stop_reason": None}
    msg = overlay._format_turn_error(payload)
    assert isinstance(msg, str)
    assert len(msg) > 0


# ── Window-only (active-window capture) toggle ────────────────────────────────

def test_toggle_window_shot_flips_state(overlay):
    before = overlay.window_shot
    overlay.toggle_window_shot()
    assert overlay.window_shot is not before
    overlay.toggle_window_shot()  # restore


def test_toggle_window_shot_reflected_in_gear_menu(overlay):
    import claude_overlay as co
    overlay.window_shot = (co.SHOT_SCOPE == "window")
    overlay._paint_window_toggle()
    overlay.toggle_window_shot()
    overlay.root.update_idletasks()
    row = next(lbl for lbl, _ in overlay._gear_items() if "Window-only" in lbl)
    assert ("✓" in row) if overlay.window_shot else ("✓" not in row)
    overlay.toggle_window_shot()  # restore


def test_toggle_window_shot_adds_confirmation_line(overlay):
    overlay.toggle_window_shot()
    txt = chat_text(overlay).lower()
    assert "window" in txt or "screen" in txt
    overlay.toggle_window_shot()  # restore


def test_toggle_window_shot_drops_stale_precapture(overlay):
    # A frame grabbed under the OLD scope must not be sent after the scope changes.
    overlay._precaptured = ([{"path": "x.png", "primary": True, "index": 1}], 0.0)
    overlay.toggle_window_shot()
    assert overlay._precaptured is None
    overlay.toggle_window_shot()  # restore


# ── Read-only (permission mode) toggle ────────────────────────────────────────

def test_toggle_read_only_asks_worker_does_not_flip_yet(overlay):
    # Unlike the other toggles, state must NOT change until the worker confirms —
    # the label must never claim a safety state the CLI isn't in.
    before = overlay.read_only
    overlay.toggle_read_only()
    assert overlay.read_only == before
    calls = [c for c in overlay.worker.calls if c[0] == "set_permission_mode"]
    assert calls, "toggle must enqueue a set_permission_mode request"
    target = calls[-1][1][0]
    assert target == ("plan" if not before else overlay._full_mode)


def test_apply_permission_mode_flips_paints_and_confirms(overlay):
    import claude_overlay as co
    overlay.read_only = False
    overlay._paint_ro_toggle()
    overlay._apply_permission_mode("plan")
    overlay.root.update_idletasks()
    assert overlay.read_only is True
    # Read-only now lives in the ⚙ menu (✓ row) and tints the gear the accent color.
    row = next(lbl for lbl, _ in overlay._gear_items() if "Read-only" in lbl)
    assert "✓" in row
    assert overlay.gear.cget("fg") == co.T["accent"]
    assert "read-only" in chat_text(overlay).lower()
    overlay._apply_permission_mode(overlay._full_mode)   # back to full access
    assert overlay.read_only is False
    row2 = next(lbl for lbl, _ in overlay._gear_items() if "Read-only" in lbl)
    assert "✓" not in row2
    assert overlay.gear.cget("fg") == co.T["muted"]


def test_apply_permission_mode_unchanged_mode_is_quiet(overlay):
    # Re-confirming the mode we're already in (e.g. after a failed switch re-sync)
    # must not spam the chat.
    overlay.read_only = False
    overlay._paint_ro_toggle()
    before = chat_text(overlay)
    overlay._apply_permission_mode(overlay._full_mode)
    assert chat_text(overlay) == before


# ── ⚙ settings menu (Window-only / Shareable / Read-only consolidated) ────────

def _gear_row(overlay, name):
    return next(lbl for lbl, _ in overlay._gear_items() if name in lbl)


def test_gear_items_reflect_each_state(overlay):
    overlay.window_shot = overlay.share_visible = overlay.read_only = True
    for name in ("Window-only", "Shareable", "Read-only"):
        assert "✓" in _gear_row(overlay, name)
    overlay.window_shot = overlay.share_visible = overlay.read_only = False
    for name in ("Window-only", "Shareable", "Read-only"):
        assert "✓" not in _gear_row(overlay, name)


def test_gear_items_wire_to_toggle_handlers(overlay):
    cmds = {lbl.strip("✓ "): cmd for lbl, cmd in overlay._gear_items()}
    assert cmds["Window-only"] == overlay.toggle_window_shot
    assert cmds["Shareable"] == overlay.toggle_screen_share
    assert cmds["Read-only"] == overlay.toggle_read_only


def test_paint_gear_color_tracks_read_only(overlay):
    import claude_overlay as co
    overlay.read_only = True
    overlay._paint_gear()
    assert overlay.gear.cget("fg") == co.T["accent"]
    overlay.read_only = False
    overlay._paint_gear()
    assert overlay.gear.cget("fg") == co.T["muted"]


# ── Persisted UI state (window_shot survives a relaunch) ─────────────────────

def test_save_and_load_state_roundtrip(monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "state.json")
    co._save_state(window_shot=True)
    assert co._load_state() == {"window_shot": True}
    co._save_state(window_shot=False)        # merge/overwrite, not append
    assert co._load_state() == {"window_shot": False}


def test_load_state_corrupt_or_wrong_shape_is_empty(monkeypatch, tmp_path):
    import claude_overlay as co
    p = tmp_path / "state.json"
    monkeypatch.setattr(co, "STATE_FILE", p)
    p.write_text("{definitely not json", encoding="utf-8")
    assert co._load_state() == {}
    p.write_text('["a", "list"]', encoding="utf-8")   # valid JSON, wrong shape
    assert co._load_state() == {}


def test_toggle_window_shot_persists_choice(overlay, monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "state.json")
    overlay.window_shot = False
    overlay._paint_window_toggle()
    overlay.toggle_window_shot()              # → on
    assert co._load_state().get("window_shot") is True
    overlay.toggle_window_shot()              # → off again
    assert co._load_state().get("window_shot") is False


# ── Read-only persistence (remembered across launches) ───────────────────────

def test_startup_permission_mode_first_launch_follows_config(monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "absent.json")   # no saved state
    monkeypatch.setattr(co, "PERMISSION_MODE", "plan")
    assert co._startup_permission_mode() == (True, "plan")
    monkeypatch.setattr(co, "PERMISSION_MODE", "bypassPermissions")
    assert co._startup_permission_mode() == (False, "bypassPermissions")


def test_startup_permission_mode_saved_choice_wins(monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "state.json")
    # Remembered UNLOCK over a plan config: launch straight into full access — and
    # into bypassPermissions specifically, so the session is born bypass-capable.
    monkeypatch.setattr(co, "PERMISSION_MODE", "plan")
    co._save_state(read_only=False)
    assert co._startup_permission_mode() == (False, "bypassPermissions")
    # Remembered LOCK over a bypass config: launch straight into plan.
    monkeypatch.setattr(co, "PERMISSION_MODE", "bypassPermissions")
    co._save_state(read_only=True)
    assert co._startup_permission_mode() == (True, "plan")


def test_startup_permission_mode_garbage_state_ignored(monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(co, "PERMISSION_MODE", "plan")
    co._save_state(read_only="yes please")   # non-bool must fall back to the config default
    assert co._startup_permission_mode() == (True, "plan")


def test_apply_permission_mode_persists_confirmed_choice(overlay, monkeypatch, tmp_path):
    import claude_overlay as co
    monkeypatch.setattr(co, "STATE_FILE", tmp_path / "state.json")
    overlay.read_only = False
    overlay._paint_ro_toggle()
    overlay._apply_permission_mode("plan")
    assert co._load_state().get("read_only") is True
    overlay._apply_permission_mode("acceptEdits")
    assert co._load_state().get("read_only") is False


# ── mode chips ────────────────────────────────────────────────────────────────
#
# Read-only / Window-only / Shareable live behind the ⚙ menu, so their state used to be
# invisible without opening it (the gear colour covered Read-only alone). The strip shows a
# chip per mode that is NOT at its quiet default, so a stock overlay stays uncluttered --
# which is why the old always-on inline toggles were removed in the first place.

@pytest.fixture
def modes(overlay):
    """Set the three mode flags directly and restore them after. Direct assignment on
    purpose: toggle_read_only() only flips once the worker confirms, and these tests are
    about what the bar DRAWS for a given state, not about how the state gets there."""
    saved = {k: getattr(overlay, k) for k in overlay.MODE_CHIPS}

    def apply(**kw):
        for k in overlay.MODE_CHIPS:
            setattr(overlay, k, kw.get(k, False))
        overlay._paint_gear()          # what every toggle handler ends up calling
        overlay.root.update_idletasks()
        return overlay

    # The fixture's bar has no real width, so the terse/full decision would swing on layout
    # noise. Pin it to the roomy branch; the narrow branch has its own tests below.
    overlay._modes_fit = lambda labels: True

    yield apply
    del overlay._modes_fit
    for k, v in saved.items():
        setattr(overlay, k, v)
    overlay._paint_gear()


def _packed_chips(ov):
    """Chip texts actually on the status bar, in left-to-right bar order."""
    lbls = set(ov.mode_lbls.values())
    return [w.cget("text") for w in ov.status_frame.pack_slaves()
            if w in lbls and w.winfo_manager() == "pack"]


def test_no_mode_chips_when_everything_is_default(modes):
    """A stock overlay shows an empty strip -- the whole point of chipping only deviations."""
    ov = modes()
    assert _packed_chips(ov) == [], f"stock overlay is showing chips: {_packed_chips(ov)!r}"


@pytest.mark.parametrize("key", ["read_only", "window_shot", "share_visible"])
def test_each_mode_shows_its_own_chip(modes, key):
    ov = modes(**{key: True})
    g, lbl, _c, _w = ov.MODE_CHIPS[key]
    expected = f"{g} {lbl}"
    assert _packed_chips(ov) == [expected], f"got {_packed_chips(ov)!r}"


def test_read_only_chip_takes_the_accent_colour(modes):
    """Read-only is the safety state, so it gets the accent -- the same signal the gear
    colour already carries. The other two are informational and stay muted."""
    import claude_overlay as co
    ov = modes(read_only=True, window_shot=True)
    assert ov.mode_lbls["read_only"].cget("fg") == co.T["accent"]
    assert ov.mode_lbls["window_shot"].cget("fg") == co.T["muted"]


def test_all_three_chips_keep_a_stable_order(modes):
    """Chips are re-packed in MODE_CHIPS order, so how you switched them on cannot shuffle
    the bar. Turning them on in reverse must still read left-to-right the same way."""
    ov = modes(read_only=True, window_shot=True, share_visible=True)
    forward = _packed_chips(ov)
    assert len(forward) == 3, f"expected all three chips, got {forward!r}"

    modes()                                   # all off
    ov = modes(share_visible=True)            # ...then on in the opposite order
    modes(share_visible=True, window_shot=True)
    ov = modes(share_visible=True, window_shot=True, read_only=True)
    assert _packed_chips(ov) == forward, f"order shifted: {_packed_chips(ov)!r} vs {forward!r}"


def test_turning_a_mode_off_removes_its_chip(modes):
    ov = modes(read_only=True, window_shot=True)
    assert len(_packed_chips(ov)) == 2
    ov = modes(window_shot=True)
    g, lbl, _c, _w = ov.MODE_CHIPS["window_shot"]
    assert _packed_chips(ov) == [f"{g} {lbl}"], f"got {_packed_chips(ov)!r}"


def test_mode_chips_sit_before_the_attachment_label(modes):
    """The strip belongs next to the ⚙ it explains, not after the 📎 count."""
    ov = modes(read_only=True)
    order = ov.status_frame.pack_slaves()
    assert order.index(ov.mode_lbls["read_only"]) < order.index(ov.attach_lbl)


@pytest.mark.parametrize("painter", ["_paint_window_toggle", "_paint_share_toggle",
                                     "_paint_ro_toggle"])
def test_every_toggle_painter_refreshes_the_strip(modes, painter):
    """Each toggle handler calls its own painter, and all three funnel into _paint_gear.
    If that chain breaks, a flipped mode would leave a stale chip on the bar."""
    ov = modes()
    assert _packed_chips(ov) == []
    ov.read_only = True
    getattr(ov, painter)()
    ov.root.update_idletasks()
    assert len(_packed_chips(ov)) == 1, (
        f"{painter}() did not refresh the mode strip")


def test_modes_fit_assumes_room_before_layout(overlay):
    """Width is 1 until Tk has laid the bar out. Guessing 'no room' there would flash the
    terse chips on every start; the <Configure> that follows re-decides with a real width."""
    overlay.status_frame.winfo_width = lambda: 1
    try:
        assert overlay._modes_fit(["⊘ Read-only"]) is True
    finally:
        del overlay.status_frame.winfo_width


def test_modes_fit_says_no_on_a_narrow_bar(overlay):
    """A bar too narrow for the spelled-out chips reports no room."""
    labels = [f"{g} {l}" for g, l, _c, _w in overlay.MODE_CHIPS.values()]
    overlay.status_frame.winfo_width = lambda: 120
    try:
        assert overlay._modes_fit(labels) is False
        overlay.status_frame.winfo_width = lambda: 4000
        assert overlay._modes_fit(labels) is True
    finally:
        del overlay.status_frame.winfo_width


def test_narrow_bar_falls_back_to_glyph_only(modes, monkeypatch):
    """When the words do not fit, chips shrink to their glyph instead of being clipped off
    the edge of the bar -- a dropped chip would report the mode as OFF, which is worse than
    terse. The glyphs stay clickable and the ⚙ menu still spells everything out."""
    ov = modes(read_only=True, window_shot=True, share_visible=True)
    # Pin the width decision rather than trusting the fixture's bar to be wide: assert the
    # full form first so this test proves the fallback CHANGES something.
    monkeypatch.setattr(ov, "_modes_fit", lambda labels: True)
    ov._paint_modes()
    assert all(" " in t for t in _packed_chips(ov)), "with room, chips should spell the mode out"

    monkeypatch.setattr(ov, "_modes_fit", lambda labels: False)
    ov._paint_modes()
    ov.root.update_idletasks()

    glyphs = [g for g, _l, _c, _w in ov.MODE_CHIPS.values()]
    assert _packed_chips(ov) == glyphs, f"expected glyph-only chips, got {_packed_chips(ov)!r}"


def test_glyph_only_chips_keep_their_colours(modes, monkeypatch):
    """Shrinking to glyphs must not cost Read-only its accent -- the terse form is exactly
    when the colour is doing most of the work."""
    import claude_overlay as co
    ov = modes(read_only=True, window_shot=True)
    monkeypatch.setattr(ov, "_modes_fit", lambda labels: False)
    ov._paint_modes()
    assert ov.mode_lbls["read_only"].cget("fg") == co.T["accent"]
    assert ov.mode_lbls["window_shot"].cget("fg") == co.T["muted"]
