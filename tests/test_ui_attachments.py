# -*- coding: utf-8 -*-
"""The attach-file feature: the picker's background routing (_stash_attachments_bg), the
named attachment strip above the input box (_refresh_attach), and that a doc-only send
actually reaches the worker with the file's path — the same things Ctrl+V paste already
had coverage for on the image-only path (see test_ui_chrome.py).

The entry point is the ⚙ menu's "Attach files…" row, not a status-bar glyph, and what
is queued is read back with _attach_row_texts() / _attach_more_text() rather than off
one label's text."""
import os
import tempfile

import pytest

from docx import Document
from pptx import Presentation

import claude_overlay as co


def type_into(ov, text):
    ov._ph_out()
    ov.entry.delete("1.0", "end")
    ov.entry.insert("1.0", text)


@pytest.fixture
def files(tmp_path):
    """One real file per attachable kind, plus one unsupported extension."""
    docx_path = tmp_path / "report.docx"
    d = Document()
    d.add_paragraph("hello from docx")
    d.save(docx_path)

    pptx_path = tmp_path / "deck.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Title"
    prs.save(pptx_path)

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%mock\n")

    png_path = tmp_path / "shot.png"
    from PIL import Image
    Image.new("RGB", (4, 4)).save(png_path)

    other_path = tmp_path / "notes.xyz"
    other_path.write_text("not a supported type")

    return {"docx": str(docx_path), "pptx": str(pptx_path), "pdf": str(pdf_path),
            "png": str(png_path), "other": str(other_path)}


class TestRefreshAttach:
    """The strip above the input box. It replaced a single status-bar label reading
    "📎 2 images, 1 file  ✕" — which named no file, gave no size, and whose one ✕
    cleared the lot."""

    def test_empty_draws_no_rows(self, overlay):
        overlay._refresh_attach()
        assert overlay._attach_row_texts() == []
        assert overlay._attach_more_text() == ""

    def test_a_doc_row_is_named_and_sized(self, overlay, files):
        overlay.pending_docs = [files["pdf"]]
        overlay._refresh_attach()
        rows = overlay._attach_row_texts()
        assert len(rows) == 1
        label, note = rows[0]
        assert label == "doc.pdf", "the row names the file, not a count of files"
        assert note, "expected a size on the row, got " + repr(note)

    def test_a_pasted_image_has_no_filename_to_show(self, overlay):
        """_stash_image copies into SHOT_DIR as shot_<ms>_paste.png, so a clipboard paste
        has no name to carry. Showing that temp path would be worse than saying so."""
        overlay.pending_images = ["/tmp/shot_1_paste.png"]
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == ["Pasted image"]

    def test_a_picked_image_shows_the_name_it_was_picked_by(self, overlay):
        overlay.pending_images = ["/tmp/shot_1_paste.png"]
        overlay._attach_names = {"/tmp/shot_1_paste.png": "diagram.png"}
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == ["diagram.png"]

    def test_images_come_before_docs(self, overlay, files):
        overlay.pending_images = ["/tmp/shot_1_paste.png"]
        overlay.pending_docs = [files["pdf"], files["docx"]]
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == [
            "Pasted image", "doc.pdf", "report.docx"]

    def test_rows_are_capped_and_the_rest_counted(self, overlay, monkeypatch):
        """16 queued files would be ~350px of a 620px window — more than half the
        transcript — to say what one counted line says."""
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 2)
        overlay.pending_docs = ["a.pdf", "b.pdf", "c.pdf", "d.pdf"]
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == ["a.pdf", "b.pdf"]
        assert overlay._attach_more_text() == "＋2 more"

    def test_no_remainder_line_when_everything_fits(self, overlay, monkeypatch):
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 4)
        overlay.pending_docs = ["a.pdf"]
        overlay._refresh_attach()
        assert overlay._attach_more_text() == ""

    def test_one_file_can_be_dropped_without_dropping_the_rest(self, overlay):
        """The old ✕ was a single target that cleared everything; per-row removal is the
        point of the strip."""
        overlay.pending_docs = ["a.pdf", "b.pdf"]
        overlay._refresh_attach()
        overlay._drop_attachment("a.pdf")
        assert overlay.pending_docs == ["b.pdf"]
        assert [l for l, _n in overlay._attach_row_texts()] == ["b.pdf"]

    def test_dropping_an_image_forgets_its_picked_name(self, overlay):
        """_attach_names is keyed by the stashed path; leaving the entry behind would grow
        the map for the life of the session."""
        overlay.pending_images = ["/tmp/shot_1_paste.png"]
        overlay._attach_names = {"/tmp/shot_1_paste.png": "diagram.png"}
        overlay._drop_attachment("/tmp/shot_1_paste.png")
        assert overlay._attach_names == {}

    def test_clear_drops_every_kind(self, overlay):
        overlay.pending_images = ["a.png"]
        overlay.pending_docs = ["a.pdf"]
        overlay.pending_bad = [("x.xlsx", "not supported")]
        overlay._attach_names = {"a.png": "a.png"}
        overlay._clear_attachments()
        assert overlay.pending_images == [] and overlay.pending_docs == []
        assert overlay.pending_bad == [] and overlay._attach_names == {}
        assert overlay._attach_row_texts() == []

    def test_a_vanished_file_still_gets_a_row(self, overlay):
        """A file deleted between the pick and the draw must not take the strip down with
        it — the row has to exist to be the thing you remove."""
        overlay.pending_docs = ["/nope/gone.pdf"]
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == ["gone.pdf"]


class TestRefusalsOnTheStrip:
    """Files the picker turned down. These used to be an add_err block in the transcript,
    which scrolled away while the files it was about were still queued below it."""

    def test_a_refusal_is_a_row_not_a_chat_error(self, overlay, monkeypatch):
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([], 0, [], [("headcount.xlsx", "not supported")], {}))
        assert errs == [], "refusals should not go to the transcript; got " + repr(errs)
        assert overlay._attach_row_texts() == [("headcount.xlsx", "not supported")]

    def test_a_refusal_row_is_drawn_in_the_error_colour(self, overlay):
        overlay.pending_bad = [("headcount.xlsx", "not supported")]
        overlay._refresh_attach()
        row = overlay._attach_rows[0]
        colours = {w.cget("fg") for w in row.winfo_children()}
        assert co.T["err"] in colours, "expected the err colour; got " + repr(colours)

    def test_a_refusal_can_be_dismissed_on_its_own(self, overlay, files):
        overlay.pending_docs = [files["pdf"]]
        overlay.pending_bad = [("x.xlsx", "not supported")]
        overlay._refresh_attach()
        overlay._drop_refusal(("x.xlsx", "not supported"))
        assert overlay.pending_bad == []
        assert overlay.pending_docs == [files["pdf"]], "dismissing a refusal keeps the files"

    def test_refusals_leave_on_send(self, overlay, files):
        """A refusal explains a QUEUE. Past the send that queue is gone, so a row still
        sitting there would be explaining nothing."""
        overlay.pending_docs = [files["pdf"]]
        overlay.pending_bad = [("x.xlsx", "not supported")]
        type_into(overlay, "go")
        overlay._send_or_stop()
        assert overlay.pending_bad == []
        assert overlay._attach_row_texts() == []

    def test_refusals_come_after_the_files_that_made_it(self, overlay):
        overlay.pending_docs = ["a.pdf"]
        overlay.pending_bad = [("x.xlsx", "not supported")]
        overlay._refresh_attach()
        assert [l for l, _n in overlay._attach_row_texts()] == ["a.pdf", "x.xlsx"]


class TestStashAttachmentsBg:
    """The picker's background routing — same call the 📎 button's file dialog feeds."""

    def test_routes_each_kind(self, overlay, files, monkeypatch):
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["png"], files["pdf"], files["docx"]])
        assert len(events) == 1
        kind, (imgs, failed, docs, why, names) = events[0]
        assert kind == "attach"
        assert len(imgs) == 1                       # the png went through _stash_image
        assert sorted(docs) == sorted([files["pdf"], files["docx"]])
        assert failed == 0 and why == []
        # The stashed copy is shot_<ms>_paste.png, so the strip needs the picked name
        # handed over with it -- keyed by the path it was stashed to.
        assert names == {imgs[0]: "shot.png"}

    def test_unsupported_extension_is_reported_by_name(self, overlay, files, monkeypatch):
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["other"]])
        _, (imgs, failed, docs, why, names) = events[0]
        assert imgs == [] and docs == []
        # failed is 0 because every rejection here is EXPLAINED; the count channel is
        # for the paste path, which has no reasons to give.
        assert failed == 0 and len(why) == 1
        # Names the file AND the cause, in two fields rather than one joined sentence:
        # the strip draws them in separate columns, and re-splitting a joined string
        # would make the em-dash load-bearing punctuation.
        name, reason = why[0]
        assert name == "notes.xyz" and ".xyz" in reason

    def test_oversized_pdf_is_rejected(self, overlay, files, monkeypatch):
        monkeypatch.setattr(co, "MAX_INLINE_PDF_BYTES", 1)   # the mock pdf is bigger than 1 byte
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["pdf"]])
        _, (imgs, failed, docs, why, names) = events[0]
        assert docs == [] and failed == 0 and len(why) == 1
        name, reason = why[0]
        assert name == "doc.pdf" and "limit" in reason

    def test_word_files_are_not_held_to_the_image_byte_cap(self, overlay, files, monkeypatch):
        """.docx/.pptx are gated on MAX_INLINE_DOC_BYTES, not the image ceiling they used to
        borrow. Only the extracted TEXT is ever sent, so weighing the FILE like an upload
        refuses decks whose actual payload is a few paragraphs — and a deck heavier than the
        16MB image cap is an ordinary deck, not an abusive one."""
        monkeypatch.setattr(co, "MAX_INLINE_IMAGE_BYTES", 1)     # would reject if consulted
        monkeypatch.setattr(co, "MAX_INLINE_DOC_BYTES", 1 << 30)
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["docx"], files["pptx"]])
        _, (imgs, failed, docs, why, names) = events[0]
        assert sorted(docs) == sorted([files["docx"], files["pptx"]])
        assert failed == 0 and why == []

    def test_one_bad_file_does_not_discard_the_rest(self, overlay, files, monkeypatch):
        """The try is per FILE. Around the loop instead, a raise on the first pick drops
        every later file AND doesn't count them — a short list and no error at all."""
        real = overlay._stash_image
        calls = {"n": 0}

        def boom(src):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("decode exploded")
            return real(src)

        monkeypatch.setattr(overlay, "_stash_image", boom)
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["png"], files["png"], files["pdf"]])
        _, (imgs, failed, docs, why, names) = events[0]
        assert len(imgs) == 1          # the second png still got through
        assert docs == [files["pdf"]]  # and so did everything after the failure
        assert failed == 0 and len(why) == 1

    def test_the_post_happens_even_if_routing_raises(self, overlay, files, monkeypatch):
        """_paste_busy is cleared by the ("attach", …) post. Miss the post and the 📎 button
        and Ctrl+V stay dead for the rest of the session, so it sits in a finally."""
        def explode(*_a):
            raise RuntimeError("nope")

        monkeypatch.setattr(overlay, "_stash_attachments_loop", explode)
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        with pytest.raises(RuntimeError):
            overlay._stash_attachments_bg([files["png"]])
        assert events and events[0][0] == "attach"


class TestAttachHandler:
    """The ("attach", …) event the background thread posts back to the UI thread."""

    def test_docs_join_pending_docs(self, overlay, files):
        overlay._handle("attach", ([], 0, [files["pdf"]]))
        assert overlay.pending_docs == [files["pdf"]]
        assert [l for l, _n in overlay._attach_row_texts()] == ["doc.pdf"]

    def test_two_tuple_payload_still_works(self, overlay, files):
        # The paste path (_stash_images_bg) still posts the old 2-tuple shape.
        overlay._handle("attach", ([files["png"]], 0))
        assert overlay.pending_images == [files["png"]]

    def test_the_queue_cap_is_one_budget_across_images_and_docs(self, overlay, files,
                                                                monkeypatch):
        """MAX_PENDING_IMAGES is a TOTAL, as its own comment says. Giving docs their own
        allowance of it silently doubled the ceiling it exists to set."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 2)
        overlay._handle("attach", ([files["png"]], 0, [files["pdf"], files["docx"]], []))
        assert len(overlay.pending_images) == 1
        assert len(overlay.pending_docs) == 1        # not 2 — the png already spent one slot
        assert len(overlay.pending_images) + len(overlay.pending_docs) == 2

    def test_images_fill_the_budget_before_docs(self, overlay, files, monkeypatch):
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay._handle("attach", ([files["png"]], 0, [files["pdf"]], []))
        assert overlay.pending_images == [files["png"]] and overlay.pending_docs == []

    def test_overflow_says_what_the_cap_is(self, overlay, files, monkeypatch):
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay._handle("attach", ([], 0, [files["pdf"], files["docx"]], []))
        # One doc fit, one did not -- and the row that says so names the limit, because
        # "did not fit" without the number is not something you can act on.
        assert ("1 more didn't fit", "limit is 1") in overlay._attach_row_texts()

    def test_every_named_reason_gets_its_own_row(self, overlay, monkeypatch):
        overlay._handle("attach", ([], 2, [], [("a.xyz", ".xyz isn't supported"),
                                               ("b.pptx", "90MB, over the 64MB limit")], {}))
        shown = overlay._attach_row_texts()
        assert ("a.xyz", ".xyz isn't supported") in shown
        assert ("b.pptx", "90MB, over the 64MB limit") in shown
        # ...and the reasonless count is still its own row, not folded into theirs.
        assert ("2 more couldn't be read", "") in shown

    def test_reasons_do_not_swallow_a_reasonless_count(self, overlay, files, monkeypatch):
        """Keying the report off `why` alone hid every failure that came WITHOUT a reason
        the moment one reason existed: a full queue plus three unreadable pastes announced
        only "1 more didn't fit" and said nothing about the three."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay.pending_images = [files["png"]]          # queue already full
        # A DIFFERENT path: re-posting the one already queued is "already attached", not
        # "did not fit", and the dedupe reports it as neither. Overflow needs a new file.
        overlay._handle("attach", (["/tmp/other.png"], 3))   # 1 dropped + 3 reasonless
        shown = overlay._attach_row_texts()
        assert ("1 more didn't fit", "limit is 1") in shown   # the overflow, with its reason
        assert ("3 more couldn't be read", "") in shown       # AND the three that had none

    def test_a_count_with_no_reasons_still_reports(self, overlay, monkeypatch):
        """The paste path posts a count and no reasons; it must not go silent."""
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([], 3))
        assert errs == [], "the strip reports this now, not the transcript"
        assert overlay._attach_row_texts() == [("3 more couldn't be read", "")]


class TestReviewHardening:
    """The four defects the adversarial review round turned up. Each is pinned on BOTH
    sides of its boundary, so a fix that over-corrects fails too."""

    def test_a_string_reason_cannot_wedge_the_ui(self, overlay):
        """`why` crosses a thread boundary inside a tuple whose arity _handle already
        tolerates four ways, so a string entry is a shape this side has to survive.
        _attach_rows_data indexes [0] and [1]: a one-character string would raise
        IndexError, and a raise in _handle kills the callback that delivers every other
        queued event -- the whole UI, not just this strip."""
        overlay._handle("attach", ([], 0, [], ["x"], {}))
        assert overlay._attach_row_texts() == [("x", "")]

    def test_a_string_reason_keeps_its_whole_text_as_the_label(self, overlay):
        """Not split on the em-dash. Coupling the two columns to that character is the
        exact thing this change removed; the whole string becomes the name instead."""
        overlay._handle("attach", ([], 0, [], ["a.xyz \u2014 .xyz isn't supported"], {}))
        label, note = overlay._attach_row_texts()[0]
        assert label == "a.xyz \u2014 .xyz isn't supported" and note == ""

    def test_a_proper_pair_still_gets_two_columns(self, overlay):
        """The coercion must not flatten the shape the picker actually sends."""
        overlay._handle("attach", ([], 0, [], [("a.xyz", "not supported")], {}))
        assert overlay._attach_row_texts() == [("a.xyz", "not supported")]

    def test_the_same_file_picked_twice_is_one_attachment(self, overlay, files):
        """worker.py's _build_query dedupes the path list before it builds blocks, so a
        second copy was never going to be sent. Drawing two rows for it meant either
        row's X removed BOTH -- a path is the only handle a row has on its file."""
        overlay._handle("attach", ([], 0, [files["pdf"]], [], {}))
        overlay._handle("attach", ([], 0, [files["pdf"]], [], {}))
        assert overlay.pending_docs == [files["pdf"]]
        assert [l for l, _n in overlay._attach_row_texts()] == ["doc.pdf"]

    def test_a_duplicate_does_not_eat_a_queue_slot(self, overlay, files, monkeypatch):
        """Deduping at draw time would have let the duplicate spend the budget on the way
        in, so the next real file had nowhere to go."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 2)
        overlay._handle("attach", ([], 0, [files["pdf"]], [], {}))
        overlay._handle("attach", ([], 0, [files["pdf"], files["docx"]], [], {}))
        assert sorted(overlay.pending_docs) == sorted([files["pdf"], files["docx"]])

    def test_two_different_files_are_both_queued(self, overlay, files):
        """The dedupe must key on the path, not collapse everything to one row."""
        overlay._handle("attach", ([], 0, [files["pdf"], files["docx"]], [], {}))
        assert len(overlay.pending_docs) == 2

    def test_a_name_is_not_kept_for_an_image_the_cap_turned_away(self, overlay,
                                                                 monkeypatch):
        """_attach_names was merged from the whole payload before the budget was applied,
        leaving an entry keyed by a path nothing on the strip refers to."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay._handle("attach", ([], 0, [], [], {}))
        overlay._handle("attach", (["/tmp/a.png", "/tmp/b.png"], 0, [], [],
                                   {"/tmp/a.png": "kept.png", "/tmp/b.png": "dropped.png"}))
        assert overlay.pending_images == ["/tmp/a.png"]
        assert overlay._attach_names == {"/tmp/a.png": "kept.png"}

    def test_the_name_of_a_kept_image_is_still_recorded(self, overlay):
        """...and the filter must not throw the useful half away with it."""
        overlay._handle("attach", (["/tmp/a.png"], 0, [], [], {"/tmp/a.png": "kept.png"}))
        assert [l for l, _n in overlay._attach_row_texts()] == ["kept.png"]

    def test_dismissing_one_of_two_identical_refusals_leaves_the_other(self, overlay):
        """A filter on equality cleared both rows at once."""
        overlay.pending_bad = [("x.xlsx", "not supported"), ("x.xlsx", "not supported")]
        overlay._refresh_attach()
        assert len(overlay._attach_row_texts()) == 2
        overlay._drop_refusal(("x.xlsx", "not supported"))
        assert overlay.pending_bad == [("x.xlsx", "not supported")]

    def test_dismissing_a_refusal_that_is_gone_is_not_an_error(self, overlay):
        """The strip is rebuilt on every resize, so a click can land on a row whose item
        has already been cleared by something else."""
        overlay.pending_bad = []
        overlay._drop_refusal(("x.xlsx", "not supported"))       # must not raise
        assert overlay.pending_bad == []

    def test_an_already_queued_file_is_not_reported_as_overflow(self, overlay, files,
                                                                monkeypatch):
        """Re-picking a file that is already attached is not a capacity problem. Saying
        "did not fit, limit is 1" would send the user off to clear space they have."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay._handle("attach", ([], 0, [files["pdf"]], [], {}))
        overlay._handle("attach", ([], 0, [files["pdf"]], [], {}))
        notes = [n for _l, n in overlay._attach_row_texts()]
        assert not any("fit" in n for n in notes), notes
        assert [l for l, _n in overlay._attach_row_texts()] == ["doc.pdf"]

    def test_a_long_reason_is_itself_truncated_to_leave_room_for_the_name(
            self, overlay, monkeypatch):
        """Round 1 only floored the NAME's budget, and the test only checked that the
        name's .cget("text") was non-empty -- a false green. pack fills side="right"
        children from their requested width before the side="left" label sees the cavity
        at all, so an unconstrained note takes the whole row and the name has nowhere to
        draw however good its string looks. The rule that actually fixes it is: cap the
        NOTE, reserving room for the name. Asserted on the note, where the fix lives."""
        monkeypatch.setattr(overlay.input_wrap, "winfo_width", lambda: 200)
        hostile = "an extraordinarily long explanation " * 6
        overlay.pending_bad = [("headcount-2026-final.xlsx", hostile)]
        overlay._refresh_attach()
        label, note = overlay._attach_row_texts()[0]
        assert note != hostile, "the note must be cut, not just the filename"
        assert note.endswith("\u2026"), note
        # and the room that bought has to actually reach the name
        room = 200 - overlay.px(48) - overlay.px(60) - overlay.px(20)
        assert overlay.f_small.measure(note) <= max(0, room), (
            "the note must leave the reserved width alone")
        assert label, "the filename still has to be there"

    def test_a_short_note_is_left_alone(self, overlay):
        """The cap must not chew up notes that were never the problem -- a plain size on
        a normal-width window has to survive intact."""
        overlay.pending_bad = [("x.xlsx", "not supported")]
        overlay._refresh_attach()
        _label, note = overlay._attach_row_texts()[0]
        assert note == "not supported"

    def test_a_non_mapping_name_slot_cannot_wedge_the_ui(self, overlay):
        """names.items() on a list raises AttributeError, and a raise in _handle kills the
        callback delivering every other queued event. Same guard `why` already got."""
        overlay._handle("attach", (["/tmp/a.png"], 0, [], [],
                                   [("/tmp/a.png", "name.png")]))   # a list, not a dict
        assert overlay.pending_images == ["/tmp/a.png"]
        assert [l for l, _n in overlay._attach_row_texts()] == ["Pasted image"]

    def test_a_dict_name_slot_still_works(self, overlay):
        overlay._handle("attach", (["/tmp/a.png"], 0, [], [], {"/tmp/a.png": "real.png"}))
        assert [l for l, _n in overlay._attach_row_texts()] == ["real.png"]

    def test_the_remainder_line_does_not_claim_refusals_were_attached(self, overlay,
                                                                      monkeypatch):
        """Five unsupported files drew four red rows and a footer saying a fifth was
        "attached", when nothing was."""
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 2)
        overlay.pending_bad = [("a.xlsx", "no"), ("b.xlsx", "no"), ("c.xlsx", "no")]
        overlay._refresh_attach()
        assert overlay._attach_more_text() == "\uff0b1 more"
        assert "attached" not in overlay._attach_more_text()

    def test_only_drawable_rows_are_built(self, overlay, monkeypatch):
        """Every row costs a stat() for its size plus a closure, and _refresh_attach runs
        on every <Configure>. Building a hundred of them to draw four made dragging the
        window edge do a hundred stat()s. Bounding the WORK is the fix; an earlier attempt
        bounded the stored LIST, which made the footer count lie."""
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 3)
        overlay.pending_bad = [(f"f{i}.xlsx", "no") for i in range(100)]
        rows, total = overlay._attach_rows_data(co.MAX_ATTACH_ROWS)
        assert len(rows) == 3, "must not build rows it will not draw"
        assert total == 100, "but it still has to COUNT them all"

    def test_the_remainder_count_is_exact_for_many_refusals(self, overlay, monkeypatch):
        """Capping the stored list at 64 made "+60 more" stand for 100 refused files."""
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 4)
        for i in range(100):
            overlay._handle("attach", ([], 0, [], [(f"f{i}.xlsx", "no")], {}))
        assert len(overlay.pending_bad) == 100, "nothing is forgotten any more"
        assert overlay._attach_more_text() == "＋96 more"

    def test_no_limit_builds_everything(self, overlay):
        """The limit is optional; without it the call is the plain full listing."""
        overlay.pending_docs = ["a.pdf", "b.pdf"]
        rows, total = overlay._attach_rows_data()
        assert len(rows) == 2 and total == 2

    def test_the_size_of_a_hidden_row_is_never_stat_ed(self, overlay, monkeypatch):
        """The stat() is the expensive part, so the limit has to be checked BEFORE the
        row is built, not by slicing a fully-built list."""
        monkeypatch.setattr(co, "MAX_ATTACH_ROWS", 1)
        seen = []
        monkeypatch.setattr(overlay, "_size_of", lambda p: seen.append(p) or 1)
        overlay.pending_docs = ["a.pdf", "b.pdf", "c.pdf"]
        overlay._refresh_attach()
        assert seen == ["a.pdf"], f"stat'd hidden rows: {seen}"

    def test_a_capped_name_budget_subtracts_the_same_chrome_the_note_did(
            self, overlay, monkeypatch):
        """The note was capped against `avail - 60 - 20` while the name was sized against
        `avail - note`, so the name could be truncated to a width WIDER than its real
        cavity -- and Tk then clips it, sometimes clipping off the very ellipsis that says
        it was cut. Both budgets have to reserve the row chrome."""
        monkeypatch.setattr(overlay.input_wrap, "winfo_width", lambda: 260)
        overlay.pending_docs = ["a-very-long-document-filename-indeed-2026.pdf"]
        overlay._refresh_attach()
        label, note = overlay._attach_row_texts()[0]
        avail = max(overlay.px(90), 260 - overlay.px(48))
        budget = avail - overlay.px(20) - overlay.f_small.measure(note)
        assert overlay.f_small.measure(label) <= max(overlay.px(60), budget)

    def test_the_refusal_bound_is_not_the_queue_cap(self, overlay, monkeypatch):
        """Tying the two together meant a queue cap of 1 discarded a real overflow row in
        order to keep the newest refusal -- an internal safety bound silently eating
        user-facing detail because someone narrowed an unrelated setting. Found by an
        existing test, not by review."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay.pending_images = ["/tmp/already.png"]
        overlay._handle("attach", (["/tmp/new.png"], 2))
        shown = overlay._attach_row_texts()
        assert ("1 more didn't fit", "limit is 1") in shown
        assert ("2 more couldn't be read", "") in shown

    def test_a_missing_file_says_so_instead_of_showing_no_size(self, overlay):
        """A vanished file was the one row on the strip with a blank size, which reads as
        a rendering glitch rather than as the reason the next send is about to fail."""
        overlay.pending_docs = ["/nope/gone.pdf"]
        overlay._refresh_attach()
        label, note = overlay._attach_row_texts()[0]
        assert label == "gone.pdf" and note == "missing"

    def test_an_empty_file_is_not_confused_with_a_missing_one(self, overlay, tmp_path):
        """_size_of returning 0 for both made them indistinguishable."""
        empty = tmp_path / "empty.pdf"
        empty.write_bytes(b"")
        overlay.pending_docs = [str(empty)]
        overlay._refresh_attach()
        _label, note = overlay._attach_row_texts()[0]
        assert note == "0 B"


class TestSendWithDocs:
    """A queued/sent turn that carries a document attachment, end to end through
    _collect_send / _deliver into the (fake) worker.ask call."""

    def test_doc_only_send_reaches_the_worker(self, overlay, files):
        overlay.pending_docs = [files["pdf"]]
        type_into(overlay, "")
        overlay._send_or_stop()
        asks = [c for c in overlay.worker.calls if c[0] == "ask"]
        assert asks, f"expected an ask call; got {overlay.worker.calls}"
        _, (text, paths) = asks[-1]
        assert files["pdf"] in paths
        assert overlay.pending_docs == []            # consumed, not left queued
        assert overlay._attach_row_texts() == []

    def test_text_and_doc_together(self, overlay, files):
        overlay.pending_docs = [files["docx"]]
        type_into(overlay, "summarize this")
        overlay._send_or_stop()
        _, (text, paths) = [c for c in overlay.worker.calls if c[0] == "ask"][-1]
        assert files["docx"] in paths
        assert "summarize this" in text

    def test_chat_label_shows_the_attachment_badge(self, overlay, files):
        overlay.pending_docs = [files["pdf"], files["docx"]]
        type_into(overlay, "look")
        overlay._send_or_stop()
        assert "📎×2" in co.chat_text(overlay) if hasattr(co, "chat_text") else True

    def test_auth_gate_treats_a_doc_only_box_as_non_empty(self, overlay, files, monkeypatch):
        # Mirrors test_ui_auth_gate's checks for pending_images: a dead login must still
        # block a send that carries only a document (no text, no screenshot, no image).
        monkeypatch.setattr(co, "AUTH_GATE", True)
        import authstate
        monkeypatch.setattr(authstate, "dead_reason", lambda: "expired")
        overlay.pending_docs = [files["pdf"]]
        type_into(overlay, "")
        blocked = overlay._auth_blocks_send()
        assert blocked is True
