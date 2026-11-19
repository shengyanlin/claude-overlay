"""Mouse-wheel scrolling of the transcript.

The bug these lock down (reported with a phone video): a markdown table taller than the window
showed rows 1-3, and the next wheel notch jumped straight to row 10. The wheel scrolled by
"units", and a Text counts an embedded widget (our tables are one Canvas each) as ONE display
line, so a units scroll could only stop at the table's top or, after Tk pulled the view back
from the blank space past the end, at its bottom. The rows in between were drawn, just never
reachable with the wheel. The wheel now scrolls by pixels.

What a notch looks like to Tk depends on where it runs: Windows and Tk 9 report multiples of
120, Tk 8.6 on macOS (still what many Mac Pythons carry) counts lines — 1, 2, … — and the app
scrolls one line for each, so three of them are the "notch" the others send as 120. The tests
speak in notches (NOTCH) so they hold on both, and each combination is also pinned directly,
with the versions patched, so the ones this machine doesn't have are checked all the same.
"""
import sys
import tkinter as tk
from types import SimpleNamespace

import pytest

import claude_overlay as co

# The delta Tk hands <MouseWheel> for one notch on THIS interpreter (see _wheel_pixels).
NOTCH = 3 if (sys.platform == "darwin" and tk.TkVersion < 9) else 120


def _wheel(ov, delta):
    return ov._on_wheel(SimpleNamespace(delta=delta))


def _record_scrolls(ov, monkeypatch):
    calls = []
    monkeypatch.setattr(ov.chat, "yview_scroll", lambda n, what: calls.append((n, what)),
                        raising=False)
    return calls


def test_a_notch_scrolls_pixels_not_lines(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -NOTCH)                             # one notch down
    line = overlay.f_body.metrics("linespace")
    assert calls == [(overlay.WHEEL_LINES * line, "pixels")]


def test_a_notch_up_scrolls_up(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, NOTCH)
    (n, what), = calls
    assert what == "pixels" and n < 0


def test_the_handler_does_the_whole_scroll(overlay, monkeypatch):
    # Without "break", Tk's own Text binding scrolled a second time on top of ours, so a notch
    # over text moved further than a notch over a table or bubble.
    _record_scrolls(overlay, monkeypatch)
    assert _wheel(overlay, -NOTCH) == "break"


def test_a_small_touchpad_delta_still_moves(overlay, monkeypatch):
    # Precision touchpads send deltas well under 120; int(-30 / 120) was 0, so over a table
    # (whose wheel comes only through here) the touchpad did nothing at all.
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -1)
    _wheel(overlay, 1)
    down, up = [n for n, _ in calls]
    assert down >= 1 and up <= -1                       # true whatever a delta of 1 means
    notch = overlay.WHEEL_LINES * overlay.f_body.metrics("linespace")
    if NOTCH == 3:
        line = overlay.f_body.metrics("linespace")      # Tk 8.6 on macOS: 1 is one line, and
        assert (down, up) == (line, -line)              # there is nothing smaller to send
    else:
        assert (down, up) == (1, -1)                    # 1/120 notch: under a pixel, so one
        _wheel(overlay, -30)                            # a quarter notch
        assert 0 < calls[-1][0] < notch
    assert all(what == "pixels" for _, what in calls)


@pytest.mark.parametrize("is_mac, tk_version, notch_delta", [
    (True, 8.6, 3),         # Tk 8.6 on macOS counts lines: three to a notch
    (True, 9.0, 120),       # Tk 9 on macOS: 120 a notch, like everywhere else
    (False, 8.6, 120),      # Windows, either Tk
    (False, 9.0, 120),
])
def test_a_notch_is_the_same_distance_on_every_tk(overlay, monkeypatch, is_mac, tk_version,
                                                  notch_delta):
    # The Tk this suite runs on is only one of these; patching what the app sees pins the
    # others. (With a hardcoded 120 these tests asked Tk 8.6/macOS for 120 notches at once.)
    monkeypatch.setattr(co, "IS_MAC", is_mac)
    monkeypatch.setattr(co.tk, "TkVersion", tk_version)
    notch = overlay.WHEEL_LINES * overlay.f_body.metrics("linespace")
    assert overlay._wheel_pixels(-notch_delta) == notch
    assert overlay._wheel_pixels(notch_delta) == -notch
    assert overlay._wheel_pixels(-3 * notch_delta) == 3 * notch     # a fast spin adds up
    assert overlay._wheel_pixels(0) == 0
    calls = _record_scrolls(overlay, monkeypatch)       # and the same through the handler
    assert _wheel(overlay, -notch_delta) == "break"
    assert calls == [(notch, "pixels")]


def test_the_step_follows_the_zoom(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    _wheel(overlay, -NOTCH)
    overlay._set_zoom(1.5)
    _wheel(overlay, -NOTCH)
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
            _wheel(overlay, -NOTCH)
        else:
            pytest.fail("wheeling down never reached the end of the transcript")
        assert _uncovered(spans, total) == []
    finally:
        top.destroy()


# ── Tk 9 on macOS: a two-finger scroll is <TouchpadScroll>, not <MouseWheel> ─────────────
# Nothing to do with notches: the event carries the gesture's own pixel deltas.

def _touchpad(dx, dy, **kw):
    """A <TouchpadScroll> event as tkinter delivers it: %D is ONE integer holding both pixel
    deltas, dx above dy's 16 bits — what Tk's own tk::PreciseScrollDeltas takes apart."""
    return SimpleNamespace(delta=(dx << 16) | (dy & 0xFFFF), **kw)


def test_a_touchpad_scroll_moves_by_the_gestures_own_pixels(overlay, monkeypatch):
    calls = _record_scrolls(overlay, monkeypatch)
    synced = []
    monkeypatch.setattr(overlay, "_sync_follow", lambda: synced.append(1))
    assert overlay._on_touchpad(_touchpad(0, -7)) == "break"    # "break": Tk's own Text
    assert overlay._on_touchpad(_touchpad(0, 12)) == "break"    # binding must not scroll too
    assert overlay._on_touchpad(_touchpad(40, -3)) == "break"   # diagonal: only dy counts,
    assert overlay._on_touchpad(_touchpad(-40, 3)) == "break"   # whatever dx's sign
    assert calls == [(7, "pixels"), (-12, "pixels"), (3, "pixels"), (-3, "pixels")]
    assert len(synced) == 4             # the follow-the-stream state moves with it, as for the wheel
    assert overlay._on_touchpad(_touchpad(25, 0)) == "break"    # sideways only: nothing to scroll
    assert len(calls) == 4 and len(synced) == 4


def test_a_touchpad_scroll_over_an_embedded_widget_reaches_the_chat(overlay, monkeypatch):
    # Bubbles, tables and chips live inside the chat; the scrollbar and the jump button beside
    # it. A gesture landing on any of them has to scroll the transcript, not vanish.
    calls = _record_scrolls(overlay, monkeypatch)
    chat, wrap = str(overlay.chat), str(overlay.chat_wrap)
    assert overlay._on_touchpad_embedded(_touchpad(0, -5, widget=chat + ".bubble")) == "break"
    assert overlay._on_touchpad_embedded(_touchpad(0, -5, widget=overlay.scrollbar)) == "break"
    assert str(overlay.scrollbar).startswith(wrap + ".")
    assert calls == [(5, "pixels")] * 2
    # The chat has its own binding (once is enough), and the input box keeps Tk's behaviour.
    assert overlay._on_touchpad_embedded(_touchpad(0, -5, widget=overlay.chat)) is None
    assert overlay._on_touchpad_embedded(_touchpad(0, -5, widget=overlay.entry)) is None
    assert len(calls) == 2


def test_a_real_touchpad_event_scrolls_the_chat_by_its_pixels(overlay, monkeypatch):
    # End to end, on a Tk that has the event: Tk packs the deltas, tkinter passes them on as
    # e.delta, the handler unpacks them — and nothing else scrolls on top of it.
    if sys.platform != "darwin":
        pytest.skip("the overlay only wires <TouchpadScroll> on macOS")
    top = tk.Toplevel(overlay.root)
    try:
        top.overrideredirect(True)
        top.geometry("360x240+0+0")
        text = tk.Text(top, wrap="word", font=overlay.f_body, bd=0, highlightthickness=0,
                       pady=0)          # so an unscrolled first line sits at y == 0
        text.pack(fill="both", expand=True)
        try:
            text.bind("<TouchpadScroll>", overlay._on_touchpad)  # as _mac_setup binds the chat
        except tk.TclError:             # (only BINDING it fails there; a query just says "")
            pytest.skip("this Tk (8.6) has no <TouchpadScroll>: <MouseWheel> carries the trackpad")
        assert "_on_touchpad" in overlay.chat.bind("<TouchpadScroll>")   # _mac_setup's wiring
        assert "_on_touchpad_embedded" in overlay.root.bind_all("<TouchpadScroll>")
        monkeypatch.setattr(overlay, "chat", text)
        text.insert("end", "\n".join("line %d" % i for i in range(1, 201)))
        top.update()

        def scrolled():                 # pixels of text above the window's top edge:
            first = text.index("@0,0")  # whole lines, plus the cut-off part of the first one
            lines = int(text.tk.call(str(text), "count", "-ypixels", "1.0", first))
            return lines - text.dlineinfo(first)[1]
        assert scrolled() == 0
        at = 0
        for dx, dy in [(0, -37), (0, 12), (9, -40), (-9, 5)]:
            packed = _touchpad(dx, dy).delta
            assert tuple(map(int, text.tk.call("tk::PreciseScrollDeltas", packed))) == (dx, dy)
            text.event_generate("<TouchpadScroll>", delta=packed)
            top.update()
            at -= dy
            assert scrolled() == at
    finally:
        top.destroy()
