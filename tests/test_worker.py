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
        # A fresh worker defaults _resolved_model to config.MODEL (the alias), so before any
        # startup resolution runs the options carry the alias — unchanged old behaviour.
        w = make_worker()
        assert w._resolved_model == config.MODEL
        opts = w._make_options()
        assert hasattr(opts, "model")
        assert opts.model == config.MODEL

    def test_make_options_uses_resolved_model(self):
        # Once run() has resolved the alias to a concrete id, _make_options must pass THAT
        # id to the SDK (the streaming-alias-lag fix) — not the raw alias.
        w = make_worker()
        w._resolved_model = "claude-opus-4-8"
        opts = w._make_options()
        assert opts.model == "claude-opus-4-8"

    def test_has_cwd_attribute(self):
        w = make_worker()
        opts = w._make_options()
        assert hasattr(opts, "cwd")
        assert opts.cwd == config.WORKING_DIR

    def test_disallows_ask_user_question(self):
        # AskUserQuestion must be removed from the tool schema so the model can't call an
        # interactive question tool the overlay can't answer (which would hang the turn).
        w = make_worker()
        opts = w._make_options()
        assert hasattr(opts, "disallowed_tools")
        assert "AskUserQuestion" in (opts.disallowed_tools or [])


# ---------------------------------------------------------------------------
# 5b. _allow_tool (permission callback / interactive-tool guard)
# ---------------------------------------------------------------------------

class TestAllowTool:

    def test_denies_ask_user_question(self):
        # The run-time guard must refuse AskUserQuestion so a leaked call can't hang the
        # turn. On an SDK new enough to expose PermissionResultDeny it returns a Deny; on an
        # older SDK (no Deny type) it degrades to Allow — disallowed_tools is then the sole,
        # still-sufficient, line of defence.
        w = make_worker()
        result = asyncio.run(w._allow_tool("AskUserQuestion", {}, None))
        from worker import PermissionResultAllow
        if worker_module.PermissionResultDeny is not None:
            assert isinstance(result, worker_module.PermissionResultDeny)
        else:
            assert isinstance(result, PermissionResultAllow)

    def test_allows_ordinary_tool(self):
        # Every non-blacklisted tool is still auto-approved (bypass-disabled hosts rely on
        # this). Mode pinned explicitly: the callback's answer now depends on the ACTIVE
        # permission mode, and the test must not float with the machine's config.
        w = make_worker()
        w._permission_mode = "bypassPermissions"
        result = asyncio.run(w._allow_tool("Bash", {"command": "ls"}, None))
        from worker import PermissionResultAllow
        assert isinstance(result, PermissionResultAllow)

    def test_plan_mode_denies_exit_plan_mode(self):
        # THE read-only guarantee: in plan mode, ExitPlanMode (the tool that ASKS for
        # write access) must be denied — the blanket auto-approve would otherwise grant
        # it and silently lift read-only. Deny degrades to Allow only on an SDK too old
        # to have PermissionResultDeny (where set_permission_mode doesn't exist either).
        w = make_worker()
        w._permission_mode = "plan"
        if worker_module.PermissionResultDeny is None:
            pytest.skip("SDK has no PermissionResultDeny; runtime mode switching is off too")
        result = asyncio.run(w._allow_tool("ExitPlanMode", {}, None))
        assert isinstance(result, worker_module.PermissionResultDeny)

    def test_plan_mode_denies_any_escalation(self):
        # Plan mode runs read-only tools without consulting the callback, so ANYTHING
        # that reaches it is a request for more power — all of it must be denied.
        w = make_worker()
        w._permission_mode = "plan"
        if worker_module.PermissionResultDeny is None:
            pytest.skip("SDK has no PermissionResultDeny")
        for tool in ("Bash", "Write", "Edit", "NotebookEdit"):
            result = asyncio.run(w._allow_tool(tool, {}, None))
            assert isinstance(result, worker_module.PermissionResultDeny), tool

    def test_plan_mode_ask_user_question_keeps_specific_message(self):
        # The AskUserQuestion deny (checked first) has its own message steering the model
        # to ask inline; the read-only deny must not swallow it.
        w = make_worker()
        w._permission_mode = "plan"
        if worker_module.PermissionResultDeny is None:
            pytest.skip("SDK has no PermissionResultDeny")
        result = asyncio.run(w._allow_tool("AskUserQuestion", {}, None))
        assert "question" in (result.message or "").lower()


# ---------------------------------------------------------------------------
# 5c. Runtime permission-mode switching
# ---------------------------------------------------------------------------

class TestPermissionModeSwitch:

    def test_enqueue_set_permission_mode(self):
        w = make_worker()
        w.set_permission_mode("plan")
        kind, payload = w.req.get_nowait()
        assert (kind, payload) == ("set_permission_mode", "plan")

    def test_initial_mode_follows_config(self):
        w = make_worker()
        assert w._permission_mode == config.PERMISSION_MODE

    def test_make_options_uses_runtime_mode_not_config(self):
        # A reconnect after a runtime switch must come back in the SWITCHED mode: the
        # options builder has to read the live attribute, not the startup constant.
        w = make_worker()
        w._permission_mode = "plan"
        opts = w._make_options()
        assert opts.permission_mode == "plan"


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


# ---------------------------------------------------------------------------
# 7. _model_family / _display_model  (statusline model label)
# ---------------------------------------------------------------------------

class TestModelFamily:

    def test_extracts_family_from_concrete_ids(self):
        assert ClaudeWorker._model_family("claude-opus-4-8[1m]") == "opus"
        assert ClaudeWorker._model_family("claude-opus-4-7") == "opus"
        assert ClaudeWorker._model_family("claude-sonnet-4-6") == "sonnet"
        assert ClaudeWorker._model_family("claude-haiku-4-5-20251001") == "haiku"

    def test_extracts_family_from_aliases(self):
        assert ClaudeWorker._model_family("opus[1m]") == "opus"
        assert ClaudeWorker._model_family("sonnet") == "sonnet"

    def test_none_and_unknown(self):
        assert ClaudeWorker._model_family(None) == ""
        assert ClaudeWorker._model_family("gpt-9") == "gpt-9"


class TestDisplayModel:

    def test_prefers_resolved_when_served_lags_same_family(self):
        # The core fix: get_context_usage reports a version-lagging 4-7[1m] on a session we
        # asked to run as 4-8[1m]; both are the Opus family, so keep our resolved id.
        w = make_worker()
        w._resolved_model = "claude-opus-4-8[1m]"
        assert w._display_model(served="claude-opus-4-7[1m]") == "claude-opus-4-8[1m]"

    def test_reattaches_1m_badge_over_assistant_model(self):
        # AssistantMessage.model is authoritative for the version but drops the [1m] suffix;
        # reconciling keeps the badge.
        w = make_worker()
        w._resolved_model = "claude-opus-4-8[1m]"
        assert w._display_model(served="claude-opus-4-8") == "claude-opus-4-8[1m]"

    def test_defers_to_served_on_cross_family_override(self):
        # A genuine override (e.g. managed settings forcing Sonnet) must still surface.
        w = make_worker()
        w._resolved_model = "claude-opus-4-8[1m]"
        assert w._display_model(served="claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_no_served_returns_resolved(self):
        w = make_worker()
        w._resolved_model = "claude-opus-4-8"
        assert w._display_model() == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# 8. _do_set_model stores the switched-to model; _emit_usage shows it
# ---------------------------------------------------------------------------

class TestDoSetModelUpdatesResolved:

    def test_stores_resolved_model(self, monkeypatch):
        # Switching model must update _resolved_model so the statusline (and _make_options on
        # any later reconnect) reflect the model we switched TO — not the startup default.
        w = make_worker()
        monkeypatch.setattr(worker_module, "resolve_model", lambda m: "claude-opus-4-8[1m]")

        class FakeClient:
            async def set_model(self, m):
                self.m = m
        fc = FakeClient()

        async def drive():
            w._loop = asyncio.get_running_loop()
            async def _noop():
                return None
            w._emit_usage = _noop           # avoid needing a real client
            await w._do_set_model(fc, "opus[1m]")

        asyncio.run(drive())
        assert w._resolved_model == "claude-opus-4-8[1m]"
        assert fc.m == "claude-opus-4-8[1m]"


class TestEmitUsageModel:

    def test_emits_display_model_not_lagging_field(self):
        # _emit_usage must publish the model we run (4-8[1m]), NOT get_context_usage's
        # lagging 4-7[1m]; context % still comes from get_context_usage.
        ui_q = queue.Queue()
        w = ClaudeWorker(ui_q)
        w._resolved_model = "claude-opus-4-8[1m]"

        class FakeClient:
            async def get_context_usage(self):
                return {"model": "claude-opus-4-7[1m]", "percentage": 12}
        fc = FakeClient()

        async def drive():
            w._client = fc
            await w._emit_usage()

        asyncio.run(drive())
        items = []
        while not ui_q.empty():
            items.append(ui_q.get_nowait())
        models = [v for (k, v) in items if k == "model"]
        ctxs = [v for (k, v) in items if k == "ctx"]
        assert models == ["claude-opus-4-8[1m]"]
        assert ctxs == [12]


# ---------------------------------------------------------------------------
# 5d. bypassPermissions runtime-elevation fallback
# ---------------------------------------------------------------------------

class _FakeModeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def set_permission_mode(self, mode):
        if self.fail:
            raise RuntimeError("nope")
        self.calls.append(mode)


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


class TestBypassElevationFallback:

    def test_substitutes_accept_edits_when_not_launch_capable(self):
        # A session NOT launched with --dangerously-skip-permissions can never be
        # elevated to bypassPermissions (the CLI refuses) — the worker must switch to
        # acceptEdits instead and confirm THAT mode to the UI, not the requested one.
        w = make_worker()
        w._bypass_capable = False
        c = _FakeModeClient()
        asyncio.run(w._do_set_permission_mode(c, "bypassPermissions"))
        assert c.calls == ["acceptEdits"]
        assert w._permission_mode == "acceptEdits"
        assert ("permission_mode", "acceptEdits") in _drain(w.ui)

    def test_passes_bypass_through_when_launch_capable(self):
        w = make_worker()
        w._bypass_capable = True
        c = _FakeModeClient()
        asyncio.run(w._do_set_permission_mode(c, "bypassPermissions"))
        assert c.calls == ["bypassPermissions"]
        assert w._permission_mode == "bypassPermissions"

    def test_plan_never_substituted(self):
        # The fallback must only affect ELEVATION to bypass — locking to plan is
        # always allowed and must go through untouched.
        w = make_worker()
        w._bypass_capable = False
        c = _FakeModeClient()
        asyncio.run(w._do_set_permission_mode(c, "plan"))
        assert c.calls == ["plan"]

    def test_failure_keeps_mode_and_resyncs_ui(self):
        # On a CLI refusal the active mode must NOT change, and the UI must get the
        # UNCHANGED mode back so the toggle repaints truthfully (plus an error line).
        w = make_worker()
        w._bypass_capable = True
        before = w._permission_mode
        asyncio.run(w._do_set_permission_mode(_FakeModeClient(fail=True), "plan"))
        assert w._permission_mode == before
        events = _drain(w.ui)
        assert ("permission_mode", before) in events
        assert any(k == "error" for k, _ in events)
