"""Mouse-wheel scrolling of the transcript.

The bug these lock down (reported with a phone video): a markdown table taller than the window
showed rows 1-3, and the next wheel notch jumped straight to row 10. The wheel scrolled by
"units", and a Text counts an embedded widget (our tables are one Canvas each) as ONE display
line, so a units scroll could only stop at the table's top or, after Tk pulled the view back
from the blank space past the end, at its bottom. The rows in between were drawn, just never
reachable with the wheel. The wheel now scrolls by pixels.
"""
import tkinter as tk
from types import SimpleNamespace

import pytest


def _wheel(ov, delta):
    return ov._on_wheel(SimpleNamespace(delta=delta))


def _record_scrolls(ov, monkeypatch):
    calls = []
    monkeypatch.setattr(ov.chat, "yview_scroll", lambda n, what: calls.append((n, what)),
                        raising=False)
    return calls


def test_a_notch_scrolls_pixels_not_lines(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -120)                               # one notch down
    line = overlay.f_body.metrics("linespace")
    assert calls == [(overlay.WHEEL_LINES * line, "pixels")]


def test_a_notch_up_scrolls_up(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, 120)
    (n, what), = calls
    assert what == "pixels" and n < 0


def test_the_handler_does_the_whole_scroll(overlay, monkeypatch):
    # Without "break", Tk's own Text binding scrolled a second time on top of ours, so a notch
    # over text moved further than a notch over a table or bubble.
    _record_scrolls(overlay, monkeypatch)
    assert _wheel(overlay, -120) == "break"


def test_a_small_touchpad_delta_still_moves(overlay, monkeypatch):
    # Precision touchpads send deltas well under 120; int(-30 / 120) was 0, so over a table
    # (whose wheel comes only through here) the touchpad did nothing at all.
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -30)
    _wheel(overlay, -1)
    _wheel(overlay, 1)
    assert [n for n, _ in calls][0] > 0
    assert [n for n, _ in calls][1:] == [1, -1]
    assert all(what == "pixels" for _, what in calls)


def test_the_step_follows_the_zoom(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -120)
    overlay._set_zoom(1.5)
    _wheel(overlay, -120)
    assert calls[1][0] > calls[0][0]


def _uncovered(spans, total):
    """Pixel ranges of [0, total) that no (top, bottom) span ever covered."""
    gaps, at = [], 0
    for top, bottom in sorted(spans):
        if top > at:
            gaps.append((at, top))
        at = max(at, bottom)
    if at < total:
        gaps.append((at, total))
    return gaps


def test_every_row_of_a_tall_table_can_be_wheeled_into_view(overlay, monkeypatch):
    # Real geometry: the shared root is withdrawn, so build a small mapped window holding a
    # Text with a REAL table (built by _build_table) far taller than the window, then wheel
    # through it with the real handler and record which slice of the table was on screen.
    top = tk.Toplevel(overlay.root)
    try:
        top.overrideredirect(True)
        top.geometry("360x240+0+0")
        text = tk.Text(top, wrap="word", font=overlay.f_body, bd=0, highlightthickness=0)
        text.pack(fill="both", expand=True)
        top.update()
        monkeypatch.setattr(overlay, "chat", text)      # _build_table and _on_wheel use it
        text.insert("end", "Business & management\n")
        table = overlay._build_table(
            ["#", "Domain", "Covers"],
            [[str(i), "Domain %d" % i, "item %d" % i] for i in range(1, 31)])
        text.window_create("end", window=table)
        text.insert("end", "\nDecisions for you\n1. OK, or leave it?")
        top.update()
        total = int(table.cget("height"))
        assert total > 2 * text.winfo_height()          # else this test proves nothing

        text.yview_moveto(0.0)
        spans = []
        for _ in range(400):
            top.update()
            if table.winfo_ismapped():
                y0 = -table.winfo_y()                   # table px at the window's top edge
                spans.append((max(0, y0), min(total, y0 + text.winfo_height())))
            if text.yview()[1] >= 1.0:
                break
            _wheel(overlay, -120)
        else:
            pytest.fail("wheeling down never reached the end of the transcript")
        assert _uncovered(spans, total) == []
    finally:
        top.destroy()
