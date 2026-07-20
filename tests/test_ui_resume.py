"""UI feature tests for session resume:
  - _maybe_offer_resume gating (state present/absent, wrong cwd, too old)
  - the in-chat Resume button (click routing, restyling via resumed/resume_failed)
  - _persist_session on turn_done, and reset() wiping the record
  - the offer going stale once a new conversation starts
  - _age_str
"""
import time

import pytest
from conftest import chat_text

import claude_overlay as co


def _seed_last_session(sid="sess-1", **overrides):
    """Write a resumable-looking record into the (throwaway) STATE_FILE."""
    rec = {"id": sid, "ts": time.time(), "cwd": co.WORKING_DIR}
    rec.update(overrides)
    co._save_state(last_session=rec)
    return rec


# ── _age_str ─────────────────────────────────────────────────────────────────

def test_age_str_minutes():
    assert co.Overlay._age_str(0) == "1 min"          # never "0 min"
    assert co.Overlay._age_str(5 * 60) == "5 min"

def test_age_str_hours_and_days():
    assert co.Overlay._age_str(3 * 3600) == "3 h"
    assert co.Overlay._age_str(2 * 86400 + 5) == "2 d"


# ── _maybe_offer_resume gating ───────────────────────────────────────────────

def test_offer_shown_for_fresh_same_cwd_session(overlay):
    _seed_last_session()
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is not None
    assert overlay._resume_btn._ustate == "idle"
    assert "pick it up where you left off" in chat_text(overlay)

def test_no_offer_without_saved_session(overlay):
    co._save_state(last_session=None)
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is None

def test_no_offer_for_other_working_dir(overlay):
    # CLI sessions are stored per directory — a record from elsewhere can't resume here.
    _seed_last_session(cwd=r"C:\somewhere\else")
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is None

def test_no_offer_for_too_old_session(overlay):
    _seed_last_session(ts=time.time() - co.RESUME_OFFER_MAX_AGE - 60)
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is None

def test_no_offer_for_malformed_record(overlay):
    co._save_state(last_session={"ts": time.time(), "cwd": co.WORKING_DIR})  # no id
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is None
    co._save_state(last_session="sess-1")                                    # not a dict
    overlay._maybe_offer_resume()
    assert overlay._resume_btn is None


# ── the Resume button ────────────────────────────────────────────────────────

def test_click_routes_to_worker_and_goes_working(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    btn._click(None)
    assert ("resume", ("sess-42",)) in overlay.worker.calls
    assert btn._ustate == "working"

def test_click_ignored_while_busy(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    overlay.busy = True
    overlay._resume_btn._click(None)
    assert all(name != "resume" for name, _ in overlay.worker.calls)
    assert overlay._resume_btn._ustate == "idle"

def test_second_click_while_working_is_inert(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    btn._click(None)
    btn._click(None)
    assert sum(1 for name, _ in overlay.worker.calls if name == "resume") == 1

def test_resumed_event_restyles_and_announces(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    overlay._session_id = "sess-42"
    overlay._handle("resumed", None)
    assert btn._ustate == "done"
    assert overlay._resume_btn is None            # no longer actionable
    assert "Resumed your last conversation" in chat_text(overlay)

def test_resume_failed_event_restyles(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    overlay._handle("resume_failed", None)
    assert btn._ustate == "failed"
    assert overlay._resume_btn is None

def test_resume_lost_event_corrects_the_claim(overlay):
    # The worker reported the CLI silently started fresh after an optimistic "resumed":
    # the button flips to failed and the chat says the context isn't actually back.
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    overlay._handle("resume_lost", None)
    assert btn._ustate == "failed"
    assert overlay._resume_btn is None
    assert "couldn't be restored" in chat_text(overlay)

def test_offer_goes_stale_when_new_conversation_starts(overlay):
    _seed_last_session("sess-42")
    overlay._maybe_offer_resume()
    btn = overlay._resume_btn
    overlay.auto_shot = False                     # keep the send path off real capture
    overlay.entry.insert("1.0", "hello")
    overlay._ph_active = False
    overlay._send_or_stop()
    assert ("ask" in {name for name, _ in overlay.worker.calls})
    assert btn._ustate == "stale"
    assert overlay._resume_btn is None
    # a stale button must not route clicks anymore
    btn._click(None)
    assert all(name != "resume" for name, _ in overlay.worker.calls)


# ── persistence ──────────────────────────────────────────────────────────────

def test_turn_done_persists_current_session(overlay):
    overlay._handle("session", "sess-9")
    overlay._handle("turn_done", None)
    saved = co._load_state().get("last_session")
    assert isinstance(saved, dict)
    assert saved["id"] == "sess-9"
    assert saved["cwd"] == co.WORKING_DIR
    assert abs(time.time() - saved["ts"]) < 5

def test_turn_done_without_session_keeps_existing_record(overlay):
    rec = _seed_last_session("sess-old")
    overlay._session_id = None
    overlay._handle("turn_done", None)            # e.g. an errored turn before init
    assert co._load_state().get("last_session")["id"] == rec["id"]

def test_reset_wipes_the_record(overlay):
    _seed_last_session("sess-9")
    overlay._session_id = "sess-9"
    overlay.reset()
    assert co._load_state().get("last_session") is None
    assert overlay._session_id is None

def test_clear_race_stale_events_dont_resurrect(overlay):
    # A turn's (session / turn_done) batch enqueued just before Clear must not re-set the
    # id or re-persist the record while the discard is pending — the worker's reset_done
    # hasn't drained yet. (#5 review, finding 2.)
    overlay._handle("session", "sess-live")
    overlay._handle("turn_done", None)
    assert co._load_state()["last_session"]["id"] == "sess-live"

    overlay.reset()                               # user clicks Clear
    assert overlay._discard_pending is True
    assert co._load_state().get("last_session") is None

    overlay._handle("session", "sess-live")       # stale batch drains AFTER the click
    overlay._handle("turn_done", None)
    assert overlay._session_id is None            # not resurrected
    assert co._load_state().get("last_session") is None

    overlay._handle("reset_done", None)           # worker confirms the wipe
    assert overlay._discard_pending is False
    overlay._handle("session", "sess-new")        # a genuine new turn persists again
    overlay._handle("turn_done", None)
    assert co._load_state()["last_session"]["id"] == "sess-new"
