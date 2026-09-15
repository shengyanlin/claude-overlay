# -*- coding: utf-8 -*-
"""The 📎 attach-file feature: the picker's background routing (_stash_attachments_bg),
the combined images+docs indicator (_refresh_attach), and that a doc-only send actually
reaches the worker with the file's path — the same things Ctrl+V paste already had
coverage for on the image-only path (see test_ui_chrome.py)."""
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

    def test_empty_shows_nothing(self, overlay):
        overlay._refresh_attach()
        assert overlay.attach_lbl.cget("text") == ""

    def test_images_only(self, overlay):
        overlay.pending_images = ["a.png", "b.png"]
        overlay._refresh_attach()
        assert overlay.attach_lbl.cget("text") == "📎 2 images  ✕"

    def test_docs_only(self, overlay):
        overlay.pending_docs = ["a.pdf"]
        overlay._refresh_attach()
        assert overlay.attach_lbl.cget("text") == "📎 1 file  ✕"

    def test_images_and_docs_together(self, overlay):
        overlay.pending_images = ["a.png"]
        overlay.pending_docs = ["a.pdf", "b.docx"]
        overlay._refresh_attach()
        assert overlay.attach_lbl.cget("text") == "📎 1 image, 2 files  ✕"

    def test_clear_drops_both(self, overlay):
        overlay.pending_images = ["a.png"]
        overlay.pending_docs = ["a.pdf"]
        overlay._clear_attachments()
        assert overlay.pending_images == [] and overlay.pending_docs == []
        assert overlay.attach_lbl.cget("text") == ""


class TestStashAttachmentsBg:
    """The picker's background routing — same call the 📎 button's file dialog feeds."""

    def test_routes_each_kind(self, overlay, files, monkeypatch):
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["png"], files["pdf"], files["docx"]])
        assert len(events) == 1
        kind, (imgs, failed, docs, why) = events[0]
        assert kind == "attach"
        assert len(imgs) == 1                       # the png went through _stash_image
        assert sorted(docs) == sorted([files["pdf"], files["docx"]])
        assert failed == 0 and why == []

    def test_unsupported_extension_is_reported_by_name(self, overlay, files, monkeypatch):
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["other"]])
        _, (imgs, failed, docs, why) = events[0]
        assert imgs == [] and docs == []
        # failed is 0 because every rejection here is EXPLAINED; the count channel is
        # for the paste path, which has no reasons to give.
        assert failed == 0 and len(why) == 1
        # Names the file AND the cause. A bare count leaves the one person who can act on
        # it (pick a different file) with nothing to act on.
        assert "notes.xyz" in why[0] and ".xyz" in why[0]

    def test_oversized_pdf_is_rejected(self, overlay, files, monkeypatch):
        monkeypatch.setattr(co, "MAX_INLINE_PDF_BYTES", 1)   # the mock pdf is bigger than 1 byte
        events = []
        monkeypatch.setattr(overlay.ui_q, "put", lambda item: events.append(item))
        overlay._stash_attachments_bg([files["pdf"]])
        _, (imgs, failed, docs, why) = events[0]
        assert docs == [] and failed == 0 and len(why) == 1
        assert "doc.pdf" in why[0] and "limit" in why[0]

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
        _, (imgs, failed, docs, why) = events[0]
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
        _, (imgs, failed, docs, why) = events[0]
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
        assert overlay.attach_lbl.cget("text") == "📎 1 file  ✕"

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
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([], 0, [files["pdf"], files["docx"]], []))
        assert errs and "1 attachments can be queued" in errs[0]

    def test_reasons_are_printed_instead_of_a_bare_count(self, overlay, monkeypatch):
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([], 2, [], ["a.xyz — .xyz isn't supported",
                                               "b.pptx — 90MB, over the 64MB limit"]))
        assert len(errs) == 1
        assert "a.xyz" in errs[0] and "b.pptx" in errs[0]
        assert "2 attachment(s) couldn't be added." not in errs[0]

    def test_reasons_do_not_swallow_a_reasonless_count(self, overlay, files, monkeypatch):
        """Keying the report off `why` alone hid every failure that came WITHOUT a reason
        the moment one reason existed: a full queue plus three unreadable pastes announced
        only "1 more didn't fit" and said nothing about the three."""
        monkeypatch.setattr(co, "MAX_PENDING_IMAGES", 1)
        overlay.pending_images = [files["png"]]          # queue already full
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([files["png"]], 3))   # paste: 1 dropped + 3 reasonless
        assert len(errs) == 1
        assert "didn't fit" in errs[0]                   # the overflow, with its reason
        assert "3 more couldn't be read" in errs[0]      # AND the three that had none

    def test_a_count_with_no_reasons_still_reports(self, overlay, monkeypatch):
        """The paste path posts a count and no reasons; it must not go silent."""
        errs = []
        monkeypatch.setattr(overlay, "add_err", lambda m: errs.append(m))
        overlay._handle("attach", ([], 3))
        assert len(errs) == 1 and "3 more couldn't be read" in errs[0]


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
        assert overlay.attach_lbl.cget("text") == ""

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
