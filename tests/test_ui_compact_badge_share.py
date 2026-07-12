"""UI feature tests for:
  - _format_compact_result
  - Compact state machine (_start_compact_anim / _compact_tick / _stop_compact_anim)
  - compact_now() guards
  - Task-done badge (_maybe_flag_done / _set_task_badge)
  - Screen-share toggle (toggle_screen_share / _paint_share_toggle)
  - Auto-shot toggle (toggle_auto / _paint_screen_toggle)
  - Turn-error formatting (_format_turn_error)
"""
import pytest
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


def test_toggle_screen_share_label_text_reflects_state(overlay):
    # Force a known state first
    import claude_overlay as co
    overlay.share_visible = co.SHOW_IN_SCREEN_SHARE_DEFAULT
    overlay._paint_share_toggle()
    overlay.toggle_screen_share()
    overlay.root.update_idletasks()
    txt = overlay.toggle_share.cget("text")
    if overlay.share_visible:
        assert "◉" in txt and "Shareable" in txt
    else:
        assert "○" in txt and "Shareable" in txt
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
