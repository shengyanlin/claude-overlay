# -*- coding: utf-8 -*-
"""
Unit tests for ClaudeWorker (worker.py).

SAFETY: We NEVER call .start(), .run(), .shutdown(), or any async/connect method.
We only construct ClaudeWorker(queue.Queue()) and exercise pure/sync/static methods.
No network, no CLI, no GUI.
"""

import asyncio
import queue
import pytest

import worker as worker_module
from worker import ClaudeWorker
import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_worker():
    """Return a fresh ClaudeWorker backed by a plain Queue (no debug tap)."""
    return ClaudeWorker(queue.Queue())


# ---------------------------------------------------------------------------
# 1. Enqueue helpers
# ---------------------------------------------------------------------------

class TestEnqueueHelpers:

    def test_ask_no_images(self):
        w = make_worker()
        w.ask("hi")
        assert w.req.get_nowait() == ("ask", ("hi", []))

    def test_ask_with_images(self):
        w = make_worker()
        w.ask("hi", ["/tmp/x.png"])
        assert w.req.get_nowait() == ("ask", ("hi", ["/tmp/x.png"]))

    def test_ask_image_paths_none_treated_as_empty(self):
        w = make_worker()
        w.ask("hello", None)
        assert w.req.get_nowait() == ("ask", ("hello", []))

    def test_reset(self):
        w = make_worker()
        w.reset()
        assert w.req.get_nowait() == ("reset", None)

    def test_compact(self):
        w = make_worker()
        w.compact()
        assert w.req.get_nowait() == ("compact", None)

    def test_set_model(self):
        w = make_worker()
        w.set_model("sonnet")
        assert w.req.get_nowait() == ("set_model", "sonnet")

    def test_ask_returns_none(self):
        w = make_worker()
        result = w.ask("hi")
        w.req.get_nowait()  # drain
        assert result is None


# ---------------------------------------------------------------------------
# 2. _compact_meta
# ---------------------------------------------------------------------------

class TestCompactMeta:

    # A minimal local class whose __name__ is "SystemMessage"
    class SystemMessage:
        def __init__(self, subtype, data):
            self.subtype = subtype
            self.data = data

    def _good_msg(self):
        return self.SystemMessage(
            subtype="compact_boundary",
            data={"compact_metadata": {
                "pre_tokens": 100,
                "post_tokens": 20,
                "duration_ms": 5,
                "trigger": "manual",
            }},
        )

    def test_happy_path(self):
        result = ClaudeWorker._compact_meta(self._good_msg())
        assert result == {
            "pre_tokens": 100,
            "post_tokens": 20,
            "duration_ms": 5,
            "trigger": "manual",
        }

    def test_wrong_subtype_returns_none(self):
        msg = self.SystemMessage(subtype="other", data={
            "compact_metadata": {"pre_tokens": 1, "post_tokens": 1}
        })
        assert ClaudeWorker._compact_meta(msg) is None

    def test_data_not_dict_returns_none(self):
        msg = self.SystemMessage(subtype="compact_boundary", data="not-a-dict")
        assert ClaudeWorker._compact_meta(msg) is None

    def test_data_none_returns_none(self):
        msg = self.SystemMessage(subtype="compact_boundary", data=None)
        assert ClaudeWorker._compact_meta(msg) is None

    def test_data_missing_compact_metadata_returns_none(self):
        msg = self.SystemMessage(subtype="compact_boundary", data={"other_key": 1})
        assert ClaudeWorker._compact_meta(msg) is None

    def test_compact_metadata_not_dict_returns_none(self):
        msg = self.SystemMessage(subtype="compact_boundary",
                                 data={"compact_metadata": "string"})
        assert ClaudeWorker._compact_meta(msg) is None

    def test_wrong_class_name_returns_none(self):
        """A class NOT named 'SystemMessage' must be rejected."""
        class NotASystemMessage:
            subtype = "compact_boundary"
            data = {"compact_metadata": {"pre_tokens": 10, "post_tokens": 5}}

        assert ClaudeWorker._compact_meta(NotASystemMessage()) is None

    def test_plain_object_returns_none(self):
        assert ClaudeWorker._compact_meta(object()) is None


# ---------------------------------------------------------------------------
# 3. _compact_result_signal
# ---------------------------------------------------------------------------

class TestCompactResultSignal:

    class SystemMessage:
        def __init__(self, data):
            self.data = data

    def test_success_signal(self):
        msg = self.SystemMessage(data={"compact_result": "success"})
        assert ClaudeWorker._compact_result_signal(msg) == "success"

    def test_error_string_signal(self):
        msg = self.SystemMessage(data={"compact_result": "error"})
        assert ClaudeWorker._compact_result_signal(msg) == "error"

    def test_no_compact_result_key_returns_none(self):
        msg = self.SystemMessage(data={"other": "value"})
        assert ClaudeWorker._compact_result_signal(msg) is None

    def test_data_not_dict_returns_none(self):
        msg = self.SystemMessage(data="string")
        assert ClaudeWorker._compact_result_signal(msg) is None

    def test_non_system_message_returns_none(self):
        class OtherMessage:
            data = {"compact_result": "success"}
        assert ClaudeWorker._compact_result_signal(OtherMessage()) is None

    def test_plain_object_returns_none(self):
        assert ClaudeWorker._compact_result_signal(object()) is None


# ---------------------------------------------------------------------------
# 4. _build_query
# ---------------------------------------------------------------------------

class TestBuildQuery:

    def test_no_images_returns_plain_text(self):
        w = make_worker()
        result = w._build_query("hello", [])
        assert result == "hello"

    def test_inline_disabled_returns_plain_text(self, monkeypatch):
        # worker uses `from config import *`, so IMAGE_INPUT lives directly in
        # the worker module namespace (not worker.config).
        monkeypatch.setattr(worker_module, "IMAGE_INPUT", "read")
        w = make_worker()
        result = w._build_query("hello", ["/tmp/fake.png"])
        assert result == "hello"

    def test_with_real_png_returns_async_gen(self, tmp_path):
        """With a real PNG, _build_query returns an async-generator."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        p = tmp_path / "red.png"
        Image.new("RGB", (4, 4), "red").save(str(p))

        w = make_worker()
        result = w._build_query("look", [str(p)])
        assert hasattr(result, "__anext__"), "Expected an async generator"

    def test_with_real_png_yields_image_block(self, tmp_path):
        """Drive the async-gen and confirm ONE image block (type=image, source.type=base64)."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        p = tmp_path / "red.png"
        Image.new("RGB", (4, 4), "red").save(str(p))

        w = make_worker()
        agen = w._build_query("look", [str(p)])

        async def collect():
            msgs = []
            async for m in agen:
                msgs.append(m)
            return msgs

        msgs = asyncio.run(collect())
        assert len(msgs) == 1
        content = msgs[0]["message"]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["type"] == "base64"

    def test_dedupe_same_path_yields_one_image_block(self, tmp_path):
        """Passing the same path twice should yield only ONE image block."""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        p = tmp_path / "red.png"
        Image.new("RGB", (4, 4), "red").save(str(p))

        w = make_worker()
        agen = w._build_query("look", [str(p), str(p)])

        async def collect():
            msgs = []
            async for m in agen:
                msgs.append(m)
            return msgs

        msgs = asyncio.run(collect())
        content = msgs[0]["message"]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 1

    def test_byte_cap_skips_image_and_queues_error(self, tmp_path, monkeypatch):
        """When MAX_INLINE_IMAGE_BYTES is set to 1, the PNG is skipped and
        an ('error', ...) item lands on the ui queue.

        Because 'x' is non-empty text, _build_query still returns an async-gen
        (content list has the text block); there are just no image blocks.
        """
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        p = tmp_path / "red.png"
        Image.new("RGB", (4, 4), "red").save(str(p))

        # The PNG is certainly larger than 1 byte
        monkeypatch.setattr(worker_module, "MAX_INLINE_IMAGE_BYTES", 1)

        ui_q = queue.Queue()
        w = ClaudeWorker(ui_q)
        result = w._build_query("x", [str(p)])

        # Since text "x" is present, content has the text block → async-gen returned
        assert hasattr(result, "__anext__"), "Expected async gen (text block present)"

        # Drive it to confirm no image blocks in content
        async def collect():
            msgs = []
            async for m in result:
                msgs.append(m)
            return msgs

        msgs = asyncio.run(collect())
        assert len(msgs) == 1
        content = msgs[0]["message"]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 0, "Image should have been skipped due to byte cap"
        # Text block must still be present
        text_blocks = [b for b in content if b.get("type") == "text"]
        assert len(text_blocks) == 1

        # An error must have been queued describing the skipped image
        item = ui_q.get_nowait()
        assert item[0] == "error"
        msg_text = item[1].lower()
        assert ("image" in msg_text or "couldn't" in msg_text or "sent" in msg_text)


# ---------------------------------------------------------------------------
# 5. _make_options
# ---------------------------------------------------------------------------

class TestMakeOptions:

    def test_returns_without_raising(self):
        w = make_worker()
        opts = w._make_options()
        assert opts is not None

    def test_has_model_attribute(self):
        w = make_worker()
        opts = w._make_options()
        assert hasattr(opts, "model")
        assert opts.model == config.MODEL

    def test_has_cwd_attribute(self):
        w = make_worker()
        opts = w._make_options()
        assert hasattr(opts, "cwd")
        assert opts.cwd == config.WORKING_DIR


# ---------------------------------------------------------------------------
# 6. _msg_has_tool
# ---------------------------------------------------------------------------

class TestMsgHasTool:

    def test_plain_object_returns_false(self):
        assert ClaudeWorker._msg_has_tool(object()) is False

    def test_none_returns_false(self):
        assert ClaudeWorker._msg_has_tool(None) is False

    def test_string_returns_false(self):
        assert ClaudeWorker._msg_has_tool("hello") is False

    def test_stream_event_tool_use_returns_true(self):
        """A StreamEvent with content_block_start / tool_use should return True."""
        from claude_agent_sdk import StreamEvent
        ev = StreamEvent(
            uuid="test-uuid",
            session_id="test-session",
            event={
                "type": "content_block_start",
                "content_block": {"type": "tool_use"},
            },
        )
        assert ClaudeWorker._msg_has_tool(ev) is True

    def test_stream_event_text_block_returns_false(self):
        """A StreamEvent with content_block_start / text should return False."""
        from claude_agent_sdk import StreamEvent
        ev = StreamEvent(
            uuid="test-uuid",
            session_id="test-session",
            event={
                "type": "content_block_start",
                "content_block": {"type": "text"},
            },
        )
        assert ClaudeWorker._msg_has_tool(ev) is False

    def test_assistant_message_with_tool_use_block_returns_true(self):
        """An AssistantMessage whose content list contains a ToolUseBlock → True."""
        from claude_agent_sdk import AssistantMessage, ToolUseBlock
        tub = ToolUseBlock(id="tid", name="bash", input={"command": "ls"})
        msg = AssistantMessage(content=[tub], model="claude-opus-4-8")
        assert ClaudeWorker._msg_has_tool(msg) is True

    def test_assistant_message_with_text_block_returns_false(self):
        """An AssistantMessage whose content has only a TextBlock → False (no ToolUseBlock)."""
        from claude_agent_sdk import AssistantMessage, TextBlock
        tb = TextBlock(text="hello")
        msg = AssistantMessage(content=[tb], model="claude-opus-4-8")
        assert ClaudeWorker._msg_has_tool(msg) is False
