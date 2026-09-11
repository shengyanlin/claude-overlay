# -*- coding: utf-8 -*-
"""The type-ahead line-up (_send_or_queue): Enter while a reply is streaming QUEUES the
message — the Claude Code CLI's behaviour — instead of interrupting the turn, and the
queue flushes one message per finished turn, in order.

The edges matter as much as the happy path:

- Enter must NEVER interrupt (Stop lives on the round button and Esc), and the button's
  Stop must DROP the line-up — the turn_done that follows an interrupt would otherwise
  fire the next queued message into a conversation the user just halted.
- A queued message's text is never silently lost: Stop/Clear list the texts in the
  transcript and hand the first back to an empty box; a row's ✕ hands that row's text
  back the same way.
- A rate-limited refusal holds the flush (every queued message would burn the same way)
  until the CLI's own quota event stops saying "rejected"; a dead login holds it via the
  auth watchdog's flag.

All tests drive the REAL Overlay methods against the conftest FakeWorker with
auto_shot OFF, so no test grabs the actual screen."""
import pytest

import claude_overlay as co
from conftest import chat_text


def _type(ov, text):
    ov._ph_out()
    ov.entry.delete("1.0", "end")
    ov.entry.insert("1.0", text)
    ov._ph_active = False


def _enter(ov, text):
    _type(ov, text)
    ov._send_or_queue()


def _asks(ov):
    return [c for c in ov.worker.calls if c[0] == "ask"]


@pytest.fixture
def q(overlay, monkeypatch):
    """Overlay armed for queue tests: no auto-screenshot (a real capture would grab this
    machine's screen mid-suite) and a login the gate provably accepts."""
    overlay.auto_shot = False
    monkeypatch.setattr(co.authstate, "dead_reason", lambda: None)
    overlay.worker.calls.clear()
    return overlay


# ── queueing instead of interrupting ──

def test_enter_while_busy_queues_not_interrupts(q):
    q._set_busy(True)
    _enter(q, "follow-up one")
    assert ("interrupt", ()) not in q.worker.calls
    assert not _asks(q)                          # nothing sent while the turn streams
    assert [i["text"] for i in q._queue] == ["follow-up one"]
    assert q._entry_text() == ""                 # the box is free for the next thought


def test_enter_while_idle_sends_immediately(q):
    _enter(q, "straight out")
    assert [c[1][0] for c in _asks(q)] == ["straight out"]
    assert q._queue == []


def test_queue_rows_render_and_hide(q):
    q._set_busy(True)
    _enter(q, "first")
    _enter(q, "second")
    q.root.update_idletasks()
    assert q.queue_frame.winfo_manager()         # strip is packed while non-empty
    assert len(q._queue_rows) == 2
    q._drop_queue()
    assert not q.queue_frame.winfo_manager()     # and hidden once it empties
    assert q._queue_rows == []


def test_rows_cap_with_more_line(q):
    q._set_busy(True)
    for i in range(co._QUEUE_ROWS_SHOWN + 2):
        _enter(q, f"msg {i}")
    q.root.update_idletasks()
    # capped rows + one "＋N more" line
    assert len(q._queue_rows) == co._QUEUE_ROWS_SHOWN + 1
    assert "＋2 more queued" in q._queue_rows[-1].cget("text")


def test_queue_full_keeps_text_in_box(q, monkeypatch):
    monkeypatch.setattr(co, "MAX_QUEUED", 2)
    q._set_busy(True)
    _enter(q, "one")
    _enter(q, "two")
    _enter(q, "three")
    assert [i["text"] for i in q._queue] == ["one", "two"]
    assert q._entry_text() == "three"            # NOT consumed
    assert "line-up is full" in chat_text(q)


def test_queue_messages_off_restores_interrupt(q, monkeypatch):
    monkeypatch.setattr(co, "QUEUE_MESSAGES", False)
    q._set_busy(True)
    _type(q, "kept draft")
    q._send_or_queue()
    assert ("interrupt", ()) in q.worker.calls   # the old meaning of Enter mid-turn
    assert q._queue == []
    assert q._entry_text() == "kept draft"       # an interrupt consumes nothing


# ── flushing ──

def test_turn_done_flushes_in_order(q):
    q._set_busy(True)
    _enter(q, "first")
    _enter(q, "second")
    q._handle("turn_done", None)                 # schedules the kick…
    q._queue_tick()                              # …driven deterministically here
    assert [c[1][0] for c in _asks(q)] == ["first"]
    assert q.busy                                # the flushed message is a real turn
    assert [i["text"] for i in q._queue] == ["second"]
    q._handle("turn_done", None)
    q._queue_tick()
    assert [c[1][0] for c in _asks(q)] == ["first", "second"]
    assert q._queue == []


def test_tick_waits_while_busy(q):
    q._set_busy(True)
    _enter(q, "held")
    q._queue_tick()                              # turn still streaming → no flush
    assert not _asks(q)
    assert [i["text"] for i in q._queue] == ["held"]
    assert q._queue_after is not None            # …but the poll chain stays alive
    q.root.after_cancel(q._queue_after)
    q._queue_after = None


def test_flushed_message_renders_user_bubble(q):
    q._set_busy(True)
    _enter(q, "bubble me")
    q._set_busy(False)
    q._queue_tick()
    # add_user embeds a canvas bubble; cheapest observable: the ask went out
    assert [c[1][0] for c in _asks(q)] == ["bubble me"]


# ── stop / clear / ✕ never lose text silently ──

def test_stop_drops_queue_and_restores_first(q):
    q._set_busy(True)
    _enter(q, "alpha")
    _enter(q, "beta")
    q._send_or_stop()                            # the round button while busy = Stop
    assert ("interrupt", ()) in q.worker.calls
    assert q._queue == []
    txt = chat_text(q)
    assert "2 queued message(s) were not sent" in txt
    assert "alpha" in txt and "beta" in txt      # the texts survive in the transcript
    assert q._entry_text() == "alpha"            # first one back in the (empty) box


def test_stop_keeps_a_nonempty_box(q):
    q._set_busy(True)
    _enter(q, "queued thing")
    _type(q, "half-typed draft")
    q._send_or_stop()
    assert q._entry_text() == "half-typed draft"   # the draft outranks the reclaimed text


def test_escape_idle_clears_queue(q):
    q._set_busy(True)
    _enter(q, "doomed")
    q._set_busy(False)
    assert q._on_escape() == "break"
    assert q._queue == []
    assert "were not sent" in chat_text(q)


def test_escape_busy_stops(q):
    q._set_busy(True)
    assert q._on_escape() == "break"
    assert ("interrupt", ()) in q.worker.calls


def test_row_x_returns_text_to_empty_box(q):
    q._set_busy(True)
    _enter(q, "typo herre")
    q._drop_queued(q._queue[0])
    assert q._queue == []
    assert q._entry_text() == "typo herre"       # ready to fix and re-Enter


def test_clear_drops_queue_with_note(q):
    q._set_busy(True)
    _enter(q, "orphaned")
    q.reset()
    assert q._queue == []
    assert "orphaned" in chat_text(q)            # listed in the fresh transcript


# ── holds ──

def test_rate_limited_result_holds_queue(q):
    q._set_busy(True)
    _enter(q, "will wait")
    q._handle("result", {"is_error": True, "subtype": "rate_limit_error",
                         "result": None, "stop_reason": None, "cost": None})
    q._handle("turn_done", None)
    assert q._queue_hold == "rate"
    q._queue_tick()                              # held → nothing goes out
    assert not _asks(q)
    assert q._queue_held                         # announced ⏸
    assert "on hold" in chat_text(q)
    q.root.after_cancel(q._queue_after)
    q._queue_after = None
    # the CLI's own quota event releases it
    q._handle("quota", {"status": "allowed"})
    assert q._queue_hold is None
    q._queue_tick()
    assert [c[1][0] for c in _asks(q)] == ["will wait"]


def test_dead_login_holds_queue(q):
    q._set_busy(True)
    _enter(q, "needs login")
    q._set_busy(False)
    q._auth_dead = True
    q._queue_tick()
    assert not _asks(q)
    assert "login is dead" in chat_text(q)
    q.root.after_cancel(q._queue_after)
    q._queue_after = None
    q._auth_dead = False
    q._queue_tick()
    assert [c[1][0] for c in _asks(q)] == ["needs login"]


def test_manual_send_clears_rate_hold(q):
    q._queue_hold = "rate"
    _enter(q, "try again now")                   # idle → straight send
    assert q._queue_hold is None
    assert [c[1][0] for c in _asks(q)] == ["try again now"]
