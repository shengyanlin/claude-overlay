"""Auto-scroll follow + the "jump to latest" pill.

The bug these lock down: following the end used to be re-derived on EVERY insert from
`chat.yview()[1] > 0.999`. Any content-driven drift — a throttled giant line, an embedded
table/image, a resize — read as "the user scrolled away", so the rest of a reply streamed in
below the fold with nothing on screen to say it had arrived. Following is now a mode that only
a real user scroll gesture (`_sync_follow`) turns off.
"""
from conftest import chat_text


def _fake_view(ov, monkeypatch, first, last):
    """Pretend the transcript is scrolled to (first, last) — the hidden test root has no real
    geometry, so yview() would otherwise always say 'everything fits'."""
    monkeypatch.setattr(ov.chat, "yview", lambda *a: (first, last), raising=False)


def _record_see(ov, monkeypatch):
    seen = []
    real = ov.chat.see
    monkeypatch.setattr(ov.chat, "see", lambda idx: (seen.append(idx), real(idx))[0],
                        raising=False)
    return seen


def test_streaming_keeps_following_when_the_view_drifts(overlay, monkeypatch):
    # The view is NOT pinned to the bottom (as an embedded table or a throttled giant line
    # leaves it), but the user never scrolled — following must survive.
    _fake_view(overlay, monkeypatch, 0.0, 0.5)
    seen = _record_see(overlay, monkeypatch)
    overlay.add_delta("a reply that keeps streaming\n")
    overlay.add_delta("and streaming some more\n")
    assert overlay._follow is True
    assert "end" in seen                     # it followed instead of stranding the text
    assert "and streaming some more" in chat_text(overlay)


def test_scrolling_up_stops_the_follow(overlay, monkeypatch):
    _fake_view(overlay, monkeypatch, 0.0, 0.4)
    overlay._sync_follow()                   # what a wheel/scrollbar/keyboard gesture calls
    assert overlay._follow is False
    seen = _record_see(overlay, monkeypatch)
    overlay.add_delta("output that lands below the fold\n")
    assert "end" not in seen                 # the reading position is left alone
    assert overlay._unread is True           # …but it's remembered as unseen


def test_scrolling_back_to_the_end_resumes_the_follow(overlay, monkeypatch):
    _fake_view(overlay, monkeypatch, 0.0, 0.4)
    overlay._sync_follow()
    assert overlay._follow is False
    _fake_view(overlay, monkeypatch, 0.5, 1.0)
    overlay._sync_follow()
    assert overlay._follow is True
    assert overlay._unread is False


def test_jump_pill_appears_only_when_scrolled_away(overlay, monkeypatch):
    assert overlay._jump_shown is False       # following → nothing to jump to
    _fake_view(overlay, monkeypatch, 0.0, 0.4)
    overlay._sb_last = 0.4                    # what yscrollcommand would have cached
    overlay._sync_follow()
    overlay.add_delta("new output while scrolled away\n")
    assert overlay._jump_shown is True
    assert overlay._jump.winfo_manager() == "place"


def test_jump_pill_returns_you_to_the_end(overlay, monkeypatch):
    _fake_view(overlay, monkeypatch, 0.0, 0.4)
    overlay._sb_last = 0.4
    overlay._sync_follow()
    overlay.add_delta("something you missed\n")
    assert overlay._jump_shown is True
    overlay._sb_last = 1.0                    # the jump scrolls the view to the end
    overlay._jump_to_end()
    assert overlay._follow is True
    assert overlay._unread is False
    assert overlay._jump_shown is False


def test_a_new_user_turn_snaps_back_to_the_end(overlay, monkeypatch):
    _fake_view(overlay, monkeypatch, 0.0, 0.4)
    overlay._sb_last = 0.4
    overlay._sync_follow()
    assert overlay._follow is False
    overlay.add_user("next question")         # you just sent something → follow the reply
    assert overlay._follow is True
    assert overlay._unread is False


def test_turn_end_catches_up_a_throttled_giant_line(overlay, monkeypatch):
    # A newline-free giant line throttles the per-delta scroll to ~25/s, so the last deltas can
    # be skipped; _md_finalize must still land on the end (as long as you're following).
    _fake_view(overlay, monkeypatch, 0.0, 0.95)
    overlay.add_delta("x" * (overlay.MD_LIVE_REPARSE_MAX + 10))
    seen = _record_see(overlay, monkeypatch)
    overlay._md_finalize()
    assert "end" in seen
