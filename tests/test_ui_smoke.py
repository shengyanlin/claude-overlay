"""Smoke test that validates the `overlay` fixture itself: a real Overlay builds on a
hidden Tk root, and streamed text reaches the chat widget. (The feature-specific UI
tests live in the other test_ui_*.py files.)"""
from conftest import chat_text


def test_overlay_builds(overlay):
    assert overlay.root is not None
    assert overlay.expanded is True
    assert hasattr(overlay, "chat")
    # the worker was the FakeWorker (no real thread / connection)
    assert overlay.worker.__class__.__name__ == "FakeWorker"


def test_stream_delta_reaches_chat(overlay):
    overlay.add_delta("hello from a streamed reply")
    overlay._md_finalize()
    assert "hello from a streamed reply" in chat_text(overlay)


def test_bold_markdown_gets_a_tag(overlay):
    overlay.add_delta("this is **bold** text")
    overlay._md_finalize()
    txt = chat_text(overlay)
    assert "bold" in txt
    assert "**" not in txt  # the emphasis markers are consumed, not shown literally
