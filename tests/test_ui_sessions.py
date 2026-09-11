"""UI tests for the past-conversations list.

The list is rendered as embedded canvases INSIDE the transcript, not in a window of its own
-- an overlay whose job is to stop you managing windows must not add one. So these assert on
cards in overlay.chat, the same way the resume-button tests do, and drive the exposed
handlers directly (Tk drops synthesised clicks on the withdrawn widgets this suite uses).

sessions.Store is faked here: this file is about what the UI does with rows, and
test_sessions.py already covers reading real transcript shapes off disk.
"""
import pathlib
import time

import pytest

import sessions as sessions_mod
from conftest import chat_text


# ── helpers ──────────────────────────────────────────────────────────────────

def _row(sid, title, messages, subtitle="", age=3600, thumb=None):
    """A Session as sessions.Store would hand one over. age is set through mtime rather than
    patched on, so the card renders through the real `age` property."""
    return sessions_mod.Session(sid, pathlib.Path(f"{sid}.jsonl"), title, subtitle,
                                messages, 1024, time.time() - age, thumb)


class FakeStore:
    """Records deletes; refuses the ones asked to fail."""

    def __init__(self, fail=()):
        self.deleted, self.fail = [], set(fail)

    def delete(self, session):
        if session.id in self.fail:
            return False
        self.deleted.append(session.id)
        return True


def _cards(ov):
    """Embedded session cards currently in the transcript, TOP-DOWN.

    Sorted by their position in the Text widget, not by the order `window names` happens to
    return them in: that is Tk's hash order, which for the auto-generated widget names looks
    like creation order right up until the tenth canvas of the session, when `.!canvas10`
    sorts before `.!canvas2` and "card 0" silently becomes a different card."""
    out = []
    for name in ov.chat.window_names():
        w = ov.chat.nametowidget(name)
        if hasattr(w, "_arm") and hasattr(w, "_state"):
            line, col = ov.chat.index(name).split(".")
            out.append(((int(line), int(col)), w))
    return [w for _pos, w in sorted(out, key=lambda p: p[0])]


def _folds(ov):
    return [ov.chat.nametowidget(n) for n in ov.chat.window_names()
            if hasattr(ov.chat.nametowidget(n), "_on_click")
            and not hasattr(ov.chat.nametowidget(n), "_arm")
            and not hasattr(ov.chat.nametowidget(n), "_copied")]


@pytest.fixture
def show(overlay):
    """Render rows and hand back (store, cards). Resets the transcript first so counts are
    about this test only."""
    def go(rows, store=None, session_id=None):
        overlay.reset()
        overlay.root.update_idletasks()
        overlay._session_id = session_id
        store = store or FakeStore()
        overlay._show_session_rows(store, rows)
        overlay.root.update_idletasks()
        return store
    return go


# ── listing ───────────────────────────────────────────────────────────────────

def test_each_long_conversation_gets_a_card(show, overlay):
    show([_row("a", "first thing", 12), _row("b", "second thing", 8)])
    assert len(_cards(overlay)) == 2


def test_the_live_session_is_not_offered(show, overlay):
    """Resuming the conversation you are already in would discard it. It must never appear."""
    show([_row("live", "this very chat", 20), _row("other", "an older one", 9)],
         session_id="live")
    assert len(_cards(overlay)) == 1
    assert "this very chat" not in chat_text(overlay)


def test_empty_list_says_so_instead_of_nothing(show, overlay):
    show([])
    assert _cards(overlay) == []
    assert "No earlier conversations" in chat_text(overlay)


def test_short_conversations_are_folded_away(show, overlay):
    """Throwaway sessions are the clutter the list exists to cut through, but 'short' is a
    guess about importance -- so they are folded, not hidden."""
    rows = [_row("long", "a real conversation", 12),
            _row("s1", "ok", 1), _row("s2", "hi", 2)]
    show(rows)
    assert len(_cards(overlay)) == 1, "a short conversation was shown as a full card"
    assert _folds(overlay)[-1]._label == "… 2 shorter conversations"


def test_the_fold_expands_to_the_rest(show, overlay):
    rows = [_row("long", "a real conversation", 12), _row("s1", "ok", 1), _row("s2", "hi", 2)]
    show(rows)
    folds = _folds(overlay)
    assert folds, "no fold row to click"
    folds[-1]._on_click()
    overlay.root.update_idletasks()
    assert len(_cards(overlay)) == 3, "expanding did not reveal the short conversations"


def test_no_fold_row_when_everything_is_substantial(show, overlay):
    show([_row("a", "one", 12), _row("b", "two", 30)])
    assert _folds(overlay) == [], "a fold row appeared with nothing to fold"


def test_singular_wording_for_one_short_conversation(show, overlay):
    show([_row("long", "real", 12), _row("s1", "ok", 1)])
    assert _folds(overlay)[-1]._label == "… 1 shorter conversation"


# ── resume ────────────────────────────────────────────────────────────────────

def test_clicking_a_card_resumes_that_session(show, overlay):
    show([_row("aaa", "the one I want", 12), _row("bbb", "not this", 9)])
    overlay.busy = False
    overlay.worker.calls.clear()

    _cards(overlay)[0]._on_click()

    assert ("resume", ("aaa",)) in overlay.worker.calls, (
        f"wrong session resumed: {overlay.worker.calls}")


def test_a_clicked_card_shows_it_is_working(show, overlay):
    show([_row("aaa", "the one I want", 12)])
    overlay.busy = False
    card = _cards(overlay)[0]
    card._on_click()
    assert card._state == "resuming"


def test_cannot_resume_while_a_turn_is_running(show, overlay):
    """Resuming mid-turn would tear the live conversation out from under the reply."""
    show([_row("aaa", "later", 12)])
    overlay.busy = True
    overlay.worker.calls.clear()
    try:
        _cards(overlay)[0]._on_click()
        assert not any(c[0] == "resume" for c in overlay.worker.calls)
    finally:
        overlay.busy = False


def test_a_card_only_resumes_once(show, overlay):
    show([_row("aaa", "later", 12)])
    overlay.busy = False
    card = _cards(overlay)[0]
    card._on_click()
    overlay.worker.calls.clear()
    card._on_click()
    assert not any(c[0] == "resume" for c in overlay.worker.calls)


# ── delete ────────────────────────────────────────────────────────────────────

def test_the_close_glyph_only_arms_a_confirm(show, overlay):
    """One click must not delete. Nothing is removed until the confirm is taken."""
    store = show([_row("aaa", "throwaway", 12)])
    card = _cards(overlay)[0]
    card._arm()
    assert card._state == "confirm"
    assert store.deleted == [], "deleted on the first click"


def test_confirming_deletes_the_session(show, overlay):
    store = show([_row("aaa", "throwaway", 12)])
    card = _cards(overlay)[0]
    card._arm()
    card._delete()
    assert store.deleted == ["aaa"]
    assert card._state == "gone"


def test_cancel_puts_the_card_back(show, overlay):
    store = show([_row("aaa", "keep me", 12)])
    card = _cards(overlay)[0]
    card._arm()
    card._cancel()
    assert card._state == "idle"
    assert store.deleted == []


def test_delete_does_nothing_unless_armed(show, overlay):
    """The confirm is the whole safety mechanism, so the delete path must refuse to run
    without it -- not merely be hard to reach through the UI."""
    store = show([_row("aaa", "keep me", 12)])
    card = _cards(overlay)[0]
    card._delete()
    assert store.deleted == []
    assert card._state == "idle"


def test_a_deleted_card_stops_resuming(show, overlay):
    show([_row("aaa", "throwaway", 12)])
    overlay.busy = False
    card = _cards(overlay)[0]
    card._arm()
    card._delete()
    overlay.worker.calls.clear()
    card._on_click()
    assert not any(c[0] == "resume" for c in overlay.worker.calls), (
        "a deleted conversation was still resumable")


def test_a_failed_delete_says_so_and_restores_the_card(show, overlay):
    """A locked file must not leave a card claiming '✓ Deleted' for a session still there."""
    store = show([_row("aaa", "locked", 12)], store=FakeStore(fail={"aaa"}))
    card = _cards(overlay)[0]
    card._arm()
    card._delete()
    assert card._state == "idle", "card claimed success after a failed delete"
    assert "Couldn't delete" in chat_text(overlay)


# ── scan plumbing ─────────────────────────────────────────────────────────────

def test_show_sessions_does_not_start_two_scans(overlay, monkeypatch):
    """The scan reads megabytes; double-clicking the menu item must not double the work."""
    started = []
    import claude_overlay as co
    monkeypatch.setattr(co.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: started.append(1)})())
    overlay._sessions_loading = False
    try:
        overlay.show_sessions()
        overlay.show_sessions()
        assert len(started) == 1
    finally:
        overlay._sessions_loading = False


def test_a_failed_scan_clears_the_loading_flag(overlay):
    """Otherwise the menu item is dead for the rest of the session."""
    overlay._sessions_loading = True
    overlay.ui_q.put(("sessions_failed", "boom"))
    overlay._poll()
    overlay.root.update_idletasks()
    assert overlay._sessions_loading is False
    assert "boom" in chat_text(overlay)


def test_past_conversations_is_in_the_gear_menu(overlay):
    labels = [lbl for lbl, _ in overlay._gear_items()]
    assert any("Past conversations" in l for l in labels), labels
