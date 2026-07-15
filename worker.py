# -*- coding: utf-8 -*-
"""The background Claude worker: a daemon thread running its own asyncio loop that
drives the `claude` CLI via claude-agent-sdk. Talks to the UI only through two
queues (requests in, events out). Imports config + debuglog (one-way)."""

import asyncio
import base64
import json
import os
import re
import sys
import threading
import time
import queue
from pathlib import Path

# Make sure both common `claude` install locations are on PATH, in case it was just
# installed this session (PATH not yet refreshed): the native installer drops it in
# %USERPROFILE%\.local\bin, and a global npm install in %APPDATA%\npm.
os.environ["PATH"] = os.pathsep.join(filter(None, [
    os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
    os.path.join(os.environ.get("APPDATA", ""), "npm"),
    os.environ.get("PATH", ""),
]))

# Spawn the `claude` CLI subprocess with no console window. Without this, running
# under pythonw (no console) makes Windows pop a CMD window for the console-mode CLI.
# Best-effort: if a future anyio drops/renames open_process, degrade gracefully
# (worst case a CMD window flashes) rather than crash on import.
try:
    import anyio as _anyio  # noqa: E402
    if sys.platform == "win32":
        _CREATE_NO_WINDOW = 0x08000000
        _orig_open_process = _anyio.open_process

        async def _open_process_no_window(*args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
            return await _orig_open_process(*args, **kwargs)

        _anyio.open_process = _open_process_no_window
except Exception:
    pass

from claude_agent_sdk import (  # noqa: E402
    ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock,
    ToolUseBlock, ResultMessage, StreamEvent, PermissionResultAllow,
)
# Deny result for the AskUserQuestion run-time guard (see _allow_tool). Imported
# defensively: an older SDK may lack it, in which case the guard degrades to allowing the
# tool (disallowed_tools still removes it from the schema, so it can't be called anyway).
try:
    from claude_agent_sdk import PermissionResultDeny  # noqa: E402
except Exception:  # pragma: no cover
    PermissionResultDeny = None
# Error types used to decide when the transport is broken and we should reconnect.
# Imported defensively: older/newer SDKs may not export all of them.
try:
    from claude_agent_sdk import (  # noqa: E402
        ClaudeSDKError, CLIConnectionError, CLIJSONDecodeError, ProcessError,
    )
except Exception:  # pragma: no cover
    class ClaudeSDKError(Exception): ...
    class CLIConnectionError(ClaudeSDKError): ...
    class CLIJSONDecodeError(ClaudeSDKError): ...
    class ProcessError(ClaudeSDKError): ...

from config import *
from debuglog import dbg, DEBUG_LOG, _UIQueueTap, _dbg_stream_last, _dbg_think_last
from modelresolve import resolve_model

class ClaudeWorker(threading.Thread):
    def __init__(self, ui_queue: "queue.Queue", permission_mode=None):
        super().__init__(daemon=True)
        # Tap the UI channel so the debug log captures every worker→UI event (no-op when
        # DEBUG_LOG is ""). The UI side keeps reading the raw queue.
        self.ui = _UIQueueTap(ui_queue) if DEBUG_LOG else ui_queue
        self.req: "queue.Queue" = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: ClaudeSDKClient | None = None
        self._running = True
        self._saw_stream = False
        self._lifecycle_task = None   # the in-flight connect()/disconnect() task, if any
        # Concrete id the family alias (config.MODEL) resolves to. Resolved once in run()
        # before the event loop starts; defaults to the raw alias so nothing breaks if
        # resolution is skipped or fails. See modelresolve for WHY (streaming alias lag).
        self._resolved_model = MODEL
        # The ACTIVE permission mode. Starts at the caller's launch mode (the UI passes
        # the remembered Read-only state; None → the config constant); the status-bar
        # "Read-only" toggle switches it at run time ("plan" ⇄ the full-access mode).
        # Kept here (not just CLI-side) because (a) _make_options must rebuild any
        # reconnect with the CURRENT mode, not the startup one, and (b) _allow_tool
        # must know when it's read-only (see the plan guard there).
        self._permission_mode = permission_mode or PERMISSION_MODE
        # Whether THIS session may ever be switched to bypassPermissions at run time:
        # the CLI refuses to ELEVATE a running session to bypass unless it was launched
        # with --dangerously-skip-permissions, and the SDK only adds that flag when the
        # session STARTS in bypass mode. Re-derived on every _open() (a reconnect that
        # happens while read-only relaunches without the flag).
        self._bypass_capable = (self._permission_mode == "bypassPermissions")

    def ask(self, text: str, image_paths=None):
        self.req.put(("ask", (text, list(image_paths or []))))
    def reset(self):                  self.req.put(("reset", None))
    def compact(self):                self.req.put(("compact", None))
    def shutdown(self):
        self._running = False
        # If the worker is currently AWAITING a lifecycle call (connect/disconnect), the
        # queued "stop" can't be read until that await returns (up to CONNECT_TIMEOUT).
        # Cancel the in-flight lifecycle task so the worker can wind down promptly instead
        # of leaving a daemon thread + orphaned `claude` CLI child after the UI is gone.
        loop, task = self._loop, self._lifecycle_task
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        self.req.put(("stop", None))

    def interrupt(self):
        loop, client = self._loop, self._client
        # The loop may be closed (worker finished / between restarts) — calling
        # run_coroutine_threadsafe on a closed loop raises RuntimeError straight into the
        # Tk callback (reset()/Stop don't guard it) and leaks the coroutine object.
        if not (loop and client) or loop.is_closed():
            return
        coro = self._safe_interrupt(client)
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            try:
                coro.close()
            except Exception:
                pass

    async def _safe_interrupt(self, client):
        try:
            await client.interrupt()
        except Exception:
            pass

    def set_model(self, model):
        # Go through the request queue (not run_coroutine_threadsafe) so a model switch is
        # serialized behind any queued reset/ask and can't interleave with _close() tearing
        # down the same client — which could leave a half-disconnected client or a status
        # line stuck on "switching model…".
        self.req.put(("set_model", model))

    def set_permission_mode(self, mode):
        # Queued like set_model, and for the same serialization reasons. The UI does NOT
        # flip its toggle when it calls this — it waits for the ("permission_mode", mode)
        # confirmation event, so the toggle never claims a safety state the CLI isn't in.
        self.req.put(("set_permission_mode", mode))

    async def _do_set_permission_mode(self, client, mode):
        if mode == "bypassPermissions" and not self._bypass_capable:
            # This session can't be elevated to bypass (see _bypass_capable). acceptEdits
            # is the runtime-reachable full-access equivalent HERE: anything else that
            # would prompt is auto-approved by _allow_tool once we're out of plan mode,
            # so the only practical difference is the label. The substituted mode is what
            # gets confirmed to the UI, so the in-chat notice reflects reality.
            mode = "acceptEdits"
        try:
            fn = getattr(client, "set_permission_mode", None)
            if fn is None:   # pre-0.1 SDKs: no runtime switching — keep the mode truthful
                raise RuntimeError("this claude-agent-sdk can't switch permission modes "
                                   "at run time — update it (pip install -U claude-agent-sdk)")
            try:
                await fn(mode)
            except Exception:
                # bypassPermissions can be REFUSED at run time even when we launched in it:
                # if managed settings disable it (disableBypassPermissionsMode), the CLI
                # silently launched in a non-bypass mode, so _bypass_capable (derived from
                # the REQUESTED launch mode) was a false positive. Fall back to acceptEdits —
                # the runtime-reachable full-access equivalent here (everything that would
                # prompt is auto-approved by _allow_tool outside plan) — instead of surfacing
                # an error, and remember bypass is unreachable so we skip it next time.
                if mode == "bypassPermissions":
                    self._bypass_capable = False
                    mode = "acceptEdits"
                    await fn(mode)
                else:
                    raise
            self._permission_mode = mode   # AFTER success only; also survives reconnects
            self.ui.put(("permission_mode", mode))
        except Exception as e:
            self.ui.put(("error", f"permission switch failed: {type(e).__name__}: {e}"))
            self.ui.put(("permission_mode", self._permission_mode))   # re-sync the toggle
        finally:
            self.ui.put(("status", ""))

    async def _do_set_model(self, client, model):
        try:
            # Switching also goes through the streaming transport, which lags the alias the
            # same way startup does — so resolve the alias to a concrete id here too. Run it
            # in a thread (resolve_model shells out to the CLI) so a cache-miss probe can't
            # block the event loop; fall back to the raw alias on any failure.
            resolved = model
            try:
                resolved = await self._loop.run_in_executor(None, resolve_model, model)
                if not (isinstance(resolved, str) and resolved):
                    resolved = model
            except Exception:
                resolved = model
            # Remember what we switched TO so the statusline reflects the current model
            # (get_context_usage lags the version for [1m] sessions — see _display_model).
            self._resolved_model = resolved
            await client.set_model(resolved)
            await self._emit_usage()
            self.ui.put(("status", ""))   # clear the "switching model…" notice
        except Exception as e:
            self.ui.put(("error", f"set_model failed: {type(e).__name__}: {e}"))

    async def _allow_tool(self, tool_name, input_data, context):
        # Run-time guard for interactive tools this GUI can't service. AskUserQuestion
        # blocks the turn waiting for an answer a no-TTY overlay can't supply — it should
        # already be gone via disallowed_tools (_make_options), but if it ever leaks back
        # in (a skill, a future CLI that ignores that list) DENY it here so the turn can't
        # hang for TOOL_IDLE_TIMEOUT (30 min). The deny message nudges the model to ask its
        # question inline as plain text instead, which the chat renders and the user answers
        # by typing. Falls through to allow if the SDK lacks PermissionResultDeny (then
        # disallowed_tools is the sole line of defence — still enough to stop the call).
        if tool_name in DISALLOWED_TOOLS and PermissionResultDeny is not None:
            return PermissionResultDeny(
                message="This overlay has no interactive question UI. Ask the user your "
                        "question inline as plain text and wait for their typed reply.")
        # Read-only guard: while the active mode is "plan", DENY everything this callback
        # is consulted about. Plan mode lets read-only tools run without asking, so any
        # call that reaches here is a request for MORE power — including ExitPlanMode,
        # the tool plan mode uses to ask for write access. The blanket auto-approve
        # below would grant that and silently lift read-only out from under the user;
        # denying keeps the "Read-only" toggle honest until the user flips it off.
        if self._permission_mode == "plan" and PermissionResultDeny is not None:
            return PermissionResultDeny(
                message="The user has locked this overlay READ-ONLY (plan mode). Don't "
                        "retry the call or try to exit plan mode — present your findings "
                        "as text, and mention that the user can flip the Read-only "
                        "status-bar toggle off if they want you to make the change.")
        # Auto-approve every other tool. permission_mode="bypassPermissions" already does
        # this on most machines, but managed/enterprise installs can DISABLE bypass
        # mode (managed-settings.json: disableBypassPermissionsMode), which makes the
        # CLI fall back to "default" and emit a permission prompt. The overlay is a
        # GUI with no TTY, so an unanswered prompt would just hang the turn forever
        # ("nowhere to approve"). This callback answers those prompts so the overlay
        # works regardless of the host's permission policy. The tool call still shows
        # up as a chip in the chat via the normal streaming path, so it isn't silent.
        return PermissionResultAllow()

    def _make_options(self) -> ClaudeAgentOptions:
        opts = dict(
            permission_mode=self._permission_mode, cwd=WORKING_DIR, model=self._resolved_model,
            can_use_tool=self._allow_tool,
            include_partial_messages=True,
            # exclude_dynamic_sections strips the per-turn-changing bits (cwd, git
            # status, auto-memory) out of the preset system prompt so the big static
            # prefix stays byte-stable → prompt-cache hits survive across turns.
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": SYSTEM_APPEND, "exclude_dynamic_sections": True},
        )
        opts["max_buffer_size"] = MAX_BUFFER_SIZE
        if DISALLOWED_TOOLS:
            # Remove interactive tools the overlay can't service (AskUserQuestion) from the
            # tool schema entirely, so the model can't call them and hang the turn — it asks
            # inline as text instead. See config.DISALLOWED_TOOLS + _allow_tool's guard.
            opts["disallowed_tools"] = list(DISALLOWED_TOOLS)
        if SKILLS is not None:
            # Enable Agent SDK skills (e.g. the slide-check skill). The SDK only injects the
            # Skill tool + sets setting_sources=["user","project"] when this is provided; left
            # unset the overlay discovers no skills at all. A list enables only those skills.
            opts["skills"] = SKILLS
        if STRICT_MCP_CONFIG:
            # Use ONLY the (empty) MCP servers defined here, ignoring the user's filesystem
            # config. Without this the spawned CLI loads every MCP server from ~/.claude.json
            # and injects all their tool schemas — measured at 72K tokens (36% of Haiku's
            # 200K window) on one machine with many MCP servers, gone before the first
            # message. setting_sources
            # alone does NOT stop this; the CLI loads MCP servers via a separate path.
            opts["strict_mcp_config"] = True
        # Some kwargs (max_buffer_size, can_use_tool, strict_mcp_config) only exist on newer
        # SDKs. Strip any the installed SDK rejects, one at a time, so an older install still
        # loads (with reduced features) instead of failing to construct options at all.
        droppable = ["strict_mcp_config", "max_buffer_size", "can_use_tool",
                     "include_partial_messages", "skills", "disallowed_tools"]
        while True:
            try:
                return ClaudeAgentOptions(**opts)
            except TypeError as e:
                victim = next((k for k in droppable if k in opts and k in str(e)), None)
                if victim is None:
                    victim = next((k for k in droppable if k in opts), None)
                if victim is None:
                    raise
                opts.pop(victim, None)

    def run(self):
        # Bounded auto-restart: even if _amain falls over entirely (e.g. the event loop
        # dies), bring it back so the overlay self-heals instead of becoming a zombie
        # window that never answers again.
        dbg("worker_start")
        # Resolve the family alias (e.g. "opus") to a concrete id ONCE, here in the worker
        # thread before the event loop spins up — the streaming transport otherwise runs a
        # version-behind model (see modelresolve). A cache hit / any failure is ~instant; only
        # a cache miss (first launch, or after a CLI upgrade) blocks this thread — not the UI —
        # for one short probe turn (~15s). The status note is set+cleared around it, so it's
        # invisible on the fast path but explains the rare wait. Guarded so resolution can
        # never stop the worker from starting; on failure we keep the raw alias.
        self.ui.put(("status", "finding latest model…"))
        try:
            resolved = resolve_model(MODEL)
            if isinstance(resolved, str) and resolved:
                self._resolved_model = resolved
            if self._resolved_model != MODEL:
                dbg("model_resolved", {"alias": MODEL, "id": self._resolved_model})
        except Exception as e:
            dbg("model_resolve_err", f"{type(e).__name__}: {e}")
        finally:
            self.ui.put(("status", ""))
        attempts = 0
        last_start = 0.0
        while self._running and attempts < 5:
            now = time.monotonic()
            if last_start and now - last_start > 180:
                attempts = 0           # survived a stable stretch → forget old failures, so
                                       # rare crashes spread over a long session don't add up
                                       # to a permanent "stopped" state (storm-based, not lifetime)
            last_start = now
            attempts += 1
            try:
                asyncio.run(self._amain())
                return                      # _amain returned cleanly (stop requested)
            except BaseException as e:  # pragma: no cover  (BaseException: e.g. CancelledError)
                self.ui.put(("error", f"worker restarting after: {type(e).__name__}: {e}"))
                self._client = None
                time.sleep(0.5)
            finally:
                # asyncio.run() closed this loop; null it so interrupt()/set_model() don't
                # schedule onto a dead loop before the next iteration sets a fresh one.
                self._loop = None
        if self._running:
            self.ui.put(("error", "Claude worker stopped after repeated failures — "
                                  "please restart the overlay."))

    async def _amain(self):
        self._loop = asyncio.get_running_loop()
        await self._open()
        while self._running:
            try:
                kind, payload = await self._loop.run_in_executor(None, self.req.get)
            except Exception:
                continue
            if kind == "stop":
                break
            # Each request is fully guarded: a failure here must never break the loop
            # (that would leave the UI waiting on a worker that's gone). Worst case we
            # reconnect and keep serving.
            try:
                if kind == "reset":
                    await self._close()
                    self._saw_stream = False
                    await self._open()
                    self.ui.put(("reset_done", None))
                elif kind == "ask":
                    await self._run_turn(payload)
                elif kind == "compact":
                    await self._run_compact()
                elif kind == "set_model":
                    if self._client is None:
                        self.ui.put(("error", "Not connected to Claude yet — can't switch model."))
                        self.ui.put(("status", ""))
                    else:
                        await self._do_set_model(self._client, payload)
                elif kind == "set_permission_mode":
                    if self._client is None:
                        self.ui.put(("error", "Not connected to Claude yet — can't switch permissions."))
                        self.ui.put(("status", ""))
                    else:
                        await self._do_set_permission_mode(self._client, payload)
            except asyncio.CancelledError:
                # a cancel (Stop / transport teardown) must not break the loop or be
                # mistaken for a fatal error — CancelledError is BaseException, not
                # Exception, so it would otherwise escape and kill the worker.
                self.ui.put(("turn_done", None))
            except BaseException as e:
                self.ui.put(("error", f"{type(e).__name__}: {e}"))
                self.ui.put(("turn_done", None))
                await self._reconnect()
        await self._close()

    async def _reconnect(self):
        """Tear down a broken client and stand up a fresh one so the next turn works.
        The conversation context is lost (new session), but the app stays alive instead
        of freezing on a dead transport."""
        self.ui.put(("system", "↻ Connection hiccup — reconnected with a fresh session."))
        try:
            await self._close()
        except Exception:
            pass
        self._saw_stream = False
        await self._open()

    async def _open(self):
        try:
            self._client = ClaudeSDKClient(options=self._make_options())
            self._bypass_capable = (self._permission_mode == "bypassPermissions")
            # Bound the connect: a wedged transport (TLS MITM, half-open socket, CLI stuck on
            # a prompt) would otherwise hang the worker here forever, where no reconnect/restart
            # guard can reach it. A timeout degrades to the normal "couldn't start" path.
            # Run it as a tracked task so shutdown() can cancel it (see shutdown/_lifecycle_task).
            self._lifecycle_task = asyncio.ensure_future(self._client.connect())
            try:
                await asyncio.wait_for(self._lifecycle_task, CONNECT_TIMEOUT)
            finally:
                self._lifecycle_task = None
            self.ui.put(("ready", None))
            # Context usage is informative only; do not block the first queued prompt on
            # an extra CLI round-trip during startup/reconnect.
            try:
                self._loop.create_task(self._emit_usage())
            except Exception:
                pass
        except BaseException as e:   # incl. CancelledError — _open must never propagate
            self._client = None
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
                self.ui.put(("error",
                    f"Connecting to Claude timed out after {CONNECT_TIMEOUT}s. The next "
                    "message will try again. (Check your network / `claude --version`.)"))
            elif isinstance(e, TypeError):   # ClaudeAgentOptions rejected a kwarg → SDK too old
                self.ui.put(("error",
                    f"Your claude-agent-sdk looks too old ({type(e).__name__}: {e}). "
                    "Update it:  pip install --upgrade claude-agent-sdk  (or run update.cmd)."))
            else:
                self.ui.put(("error",
                    f"Could not start Claude: {type(e).__name__}: {e}\n"
                    "Is the `claude` CLI installed and logged in? Run `claude --version` "
                    "in a terminal; if it's missing, run setup.cmd (or `irm "
                    "https://claude.ai/install.ps1 | iex`), then `claude` to /login."))

    @staticmethod
    def _model_family(m):
        """The bare family ('opus'/'sonnet'/'haiku') of a model id or alias, else the
        lowercased string. Lets us tell 'same family, the version just lags' apart from a
        genuine cross-family override."""
        m = (m or "").lower()
        for fam in ("opus", "sonnet", "haiku"):
            if fam in m:
                return fam
        return m

    def _display_model(self, served=None):
        """The model id to show in the statusline.

        Prefer self._resolved_model — the concrete id the overlay requested and, per
        verification, actually runs (e.g. 'claude-opus-4-8[1m]'): it carries the right
        VERSION *and* the [1m] context badge. Deliberately do NOT trust
        get_context_usage()['model']: on a 1M session it lags the version — it reports
        'claude-opus-4-7[1m]' while 'claude-opus-4-8' is really serving the turn (confirmed
        via AssistantMessage.model). Only defer to a `served` id from a DIFFERENT family
        (a managed-settings override, or a resolution that fell back to the raw alias and
        genuinely ran the older model) so we never hide a real override."""
        want = self._resolved_model
        if served and want and self._model_family(served) != self._model_family(want):
            return served
        return want or served

    async def _emit_usage(self):
        """Push current model + context-window usage % to the UI statusline."""
        # Capture the client we're measuring. A turn's finally schedules this against the
        # *current* client; if a Clear/reconnect swaps the client out while the (slow,
        # round-trips to the CLI) get_context_usage() is in flight, the result describes a
        # session that no longer exists. Emitting it would overwrite the fresh post-reset
        # baseline with the OLD conversation's high % — the "Clear didn't drop context" bug.
        client = self._client
        if client is None:
            return
        try:
            u = await asyncio.wait_for(client.get_context_usage(), timeout=6)
            if client is not self._client:   # reset/reconnect happened mid-flight → stale
                return
            served = u.get("model") if isinstance(u, dict) else None
            # Show the model we actually run, not get_context_usage's version-lagging field
            # (it reports 4-7[1m] on a 4-8 [1m] session — see _display_model). `served` is
            # passed only so a real cross-family override still surfaces.
            dm = self._display_model(served=served)
            if dm:
                self.ui.put(("model", dm))
            if isinstance(u, dict) and u.get("percentage") is not None:
                self.ui.put(("ctx", u["percentage"]))
        except Exception:
            pass

    async def _close(self):
        # Null the handle FIRST so a disconnect that hangs (bounded below) can't leave the
        # rest of the worker pointing at a half-dead client.
        client, self._client = self._client, None
        if client is not None:
            self._lifecycle_task = asyncio.ensure_future(client.disconnect())
            try:
                await asyncio.wait_for(self._lifecycle_task, DISCONNECT_TIMEOUT)
            except Exception:
                pass
            finally:
                self._lifecycle_task = None

    async def _run_turn(self, payload):
        text, image_paths = payload if isinstance(payload, tuple) else (payload, [])
        dbg("turn_start", f"imgs={len(image_paths or [])} | {str(text)[:120]}")
        _dbg_stream_last[0] = 0.0   # force this turn's FIRST delta to log → measures time-to-first-token
        _dbg_think_last[0] = 0.0    # and its first thinking token → time-to-first-thinking
        # ── Debug-only test hook, gated by CLAUDE_OVERLAY_DEBUG_LOG so it's inert in normal use
        #    (without the env var, `/simerror` is just sent to Claude as ordinary text). It emits a
        #    synthetic errored ResultMessage so the "last turn ended with an error (<reason>)" UI
        #    can be exercised without a real API failure. Presets: (none)=overloaded, max, exec,
        #    rate; any other word is used verbatim as the subtype.
        ts = text.strip() if isinstance(text, str) else ""
        if DEBUG_LOG and (ts == "/simerror" or ts.startswith("/simerror ")):
            arg = ts[len("/simerror"):].strip()
            presets = {
                "":     ("overloaded_error", "The model was overloaded (HTTP 529). Transient — the next turn retries."),
                "max":  ("error_max_turns", None),
                "exec": ("error_during_execution", "A tool call failed during execution."),
                "rate": ("rate_limit_error", "Rate limited (HTTP 429)."),
            }
            sub, detail = presets.get(arg, (arg, None))
            self.ui.put(("system", f"(test: simulating an errored result — subtype={sub})"))
            self.ui.put(("result", {"is_error": True, "subtype": sub, "result": detail,
                                    "stop_reason": None, "cost": None}))
            self.ui.put(("turn_done", None))
            return
        if self._client is None:        # initial connect failed earlier — try once more
            await self._open()
        if self._client is None:
            self.ui.put(("error", "Not connected to Claude. Check `claude --version`."))
            self.ui.put(("turn_done", None))
            return
        agen = None
        try:
            await asyncio.wait_for(
                self._client.query(self._build_query(text, image_paths)), QUERY_TIMEOUT)
            blocks: dict = {}
            tool_active = False
            # Iterate the stream item-by-item under an idle timeout instead of a bare
            # `async for`: if the transport goes silent forever (dead CLI, wedged socket)
            # the turn would otherwise hold "thinking…" indefinitely. A gap longer than the
            # idle budget is treated as a broken transport → reconnect. Once a tool call is in
            # flight we switch to a much longer budget so a legitimately silent long-running
            # tool (a big build/test that streams nothing for minutes) isn't mistaken for dead.
            agen = self._client.receive_response()
            while True:
                budget = TOOL_IDLE_TIMEOUT if tool_active else RECV_IDLE_TIMEOUT
                try:
                    msg = await asyncio.wait_for(agen.__anext__(), budget)
                except StopAsyncIteration:
                    break
                if not tool_active and self._msg_has_tool(msg):
                    tool_active = True
                self._dispatch(msg, blocks)
        except asyncio.CancelledError:
            # Stop button / interrupt() / transport cancel — end this turn cleanly.
            # (BaseException, so it'd otherwise escape every `except Exception` and the
            # worker thread would die permanently.) Don't reconnect; shutdown is queue-driven.
            self.ui.put(("system", "⏹ stopped."))
        except (asyncio.TimeoutError, TimeoutError):
            # query() wedged or the stream went silent past the idle budget → the transport
            # is effectively dead; rebuild it so the next turn works instead of hanging here.
            self.ui.put(("error", "Claude stopped responding — reconnecting with a fresh session."))
            await self._reconnect()
        except BaseException as e:
            self.ui.put(("error", f"{type(e).__name__}: {e}"))
            # a decode/connection/process error means the transport is dead — the client
            # is unusable now, so rebuild it before the next turn instead of erroring
            # forever (the classic "it crashed and won't respond anymore" symptom).
            if isinstance(e, (CLIJSONDecodeError, CLIConnectionError, ProcessError, ClaudeSDKError)):
                await self._reconnect()
        finally:
            # Finalize the response stream. wait_for cancelling __anext__() does NOT close the
            # async generator, so without this the SDK's reader task / stdout pipe can be left
            # half-open (a leak, or a later disconnect() that hangs). Bounded so a broken close
            # can't reintroduce a hang.
            if agen is not None:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    try:
                        await asyncio.wait_for(aclose(), DISCONNECT_TIMEOUT)
                    except BaseException:
                        pass
            self.ui.put(("turn_done", None))
            # Refresh context% off the critical path: schedule it rather than
            # awaiting, so the UI leaves "thinking…" the instant the reply ends
            # instead of after an extra round-trip.
            try:
                self._loop.create_task(self._emit_usage())
            except Exception:
                pass

    async def _run_compact(self):
        """Compact the conversation by sending the CLI's `/compact` command. Compaction
        streams only system/user messages (status → compact_boundary → the re-injected
        summary → an empty result) — no assistant answer — so we DON'T render the stream as
        chat. We just signal start/finish to the UI (which animates a CLI-style spinner) and
        capture the compact_boundary's pre/post token counts for the result line."""
        if self._client is None:        # not connected yet — try once
            await self._open()
        if self._client is None:
            self.ui.put(("compact_done",
                         {"status": "error", "meta": None,
                          "detail": "not connected (check `claude --version`)"}))
            return
        self.ui.put(("compacting", None))   # → UI starts the animation
        agen = None
        meta = None
        status = "ok"
        detail = None
        result_signal = None                # the CLI's own compact_result flag, if it sends one
        try:
            await asyncio.wait_for(self._client.query("/compact"), QUERY_TIMEOUT)
            agen = self._client.receive_response()
            while True:
                try:
                    msg = await asyncio.wait_for(agen.__anext__(), COMPACT_IDLE_TIMEOUT)
                except StopAsyncIteration:
                    break
                info = self._compact_meta(msg)
                if info is not None:
                    meta = info
                sig = self._compact_result_signal(msg)
                if sig is not None:
                    result_signal = sig
            # The stream finished cleanly — but "no exception" is NOT "it compacted". Confirm it:
            # trust the CLI's compact_result=="success" flag, OR a compact_boundary that shows the
            # context actually shrank (post < pre). Otherwise report "unconfirmed" rather than
            # claiming success (e.g. the CLI declined / nothing to compact / a silent internal fail).
            shrank = bool(meta and meta.get("pre_tokens") and meta.get("post_tokens")
                          and meta["post_tokens"] < meta["pre_tokens"])
            if result_signal == "success" or shrank:
                status = "ok"
            else:
                status = "unconfirmed"
                detail = (f"CLI reported compact_result={result_signal!r}"
                          if result_signal is not None
                          else "no success signal or token reduction seen")
        except asyncio.CancelledError:
            status = "cancelled"        # Stop / Clear interrupted it — conversation unchanged
        except (asyncio.TimeoutError, TimeoutError):
            status = "timeout"
            await self._reconnect()
        except BaseException as e:
            status = "error"
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, (CLIJSONDecodeError, CLIConnectionError, ProcessError, ClaudeSDKError)):
                await self._reconnect()
        finally:
            if agen is not None:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    try:
                        await asyncio.wait_for(aclose(), DISCONNECT_TIMEOUT)
                    except BaseException:
                        pass
            self.ui.put(("compact_done", {"status": status, "meta": meta, "detail": detail}))
            try:
                self._loop.create_task(self._emit_usage())
            except Exception:
                pass

    @staticmethod
    def _compact_meta(msg):
        """Pull {pre_tokens, post_tokens, duration_ms, trigger} from a compact_boundary
        system message (the authoritative before/after sizes), else None. Identified by
        class name so this doesn't depend on importing the SDK's message types."""
        if type(msg).__name__ != "SystemMessage":
            return None
        if getattr(msg, "subtype", None) != "compact_boundary":
            return None
        data = getattr(msg, "data", None)
        md = data.get("compact_metadata") if isinstance(data, dict) else None
        if not isinstance(md, dict):
            return None
        return {"pre_tokens": md.get("pre_tokens"), "post_tokens": md.get("post_tokens"),
                "duration_ms": md.get("duration_ms"), "trigger": md.get("trigger")}

    @staticmethod
    def _compact_result_signal(msg):
        """The CLI's own success flag: a status system message carries
        `compact_result: "success"` (or an error string) once compaction settles.
        Returns that value, or None for messages that don't carry it."""
        if type(msg).__name__ != "SystemMessage":
            return None
        data = getattr(msg, "data", None)
        if not isinstance(data, dict):
            return None
        cr = data.get("compact_result")
        return cr if cr else None

    def _build_query(self, text: str, image_paths: list):
        """Return the prompt for client.query(). With inline images we yield a
        structured user message (text + base64 image blocks) so the model sees
        the screen directly — no per-turn Read round-trip. Otherwise a plain
        string (the legacy "Read the PNG path" flow builds its own text upstream)."""
        if IMAGE_INPUT != "inline" or not image_paths:
            return text
        content: list = []
        if text:
            content.append({"type": "text", "text": text})
        failed = 0
        total = 0
        seen = set()
        for p in image_paths:
            if p in seen:           # dedupe repeated paths (same screenshot/paste twice)
                continue
            seen.add(p)
            if len(seen) > MAX_INLINE_IMAGES:   # cap count per turn
                failed += 1
                continue
            try:
                # Cap before reading: per-file AND aggregate, so a huge non-image file (per
                # file) or many accumulated attachments (aggregate) can't be read whole into
                # RAM and base64-expanded into one query.
                size = Path(p).stat().st_size
                if size > MAX_INLINE_IMAGE_BYTES or (total + size) > MAX_INLINE_TOTAL_BYTES:
                    failed += 1
                    continue
                data = Path(p).read_bytes()
            except Exception:
                failed += 1
                continue
            if not data:            # 0-byte / unreadable-as-empty → don't send a blank block
                failed += 1
                continue
            total += size
            ext = Path(p).suffix.lower()
            mt = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                  ".gif": "image/gif"}.get(ext, "image/png")
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": mt,
                "data": base64.b64encode(data).decode()}})
        if failed:   # tell the user their screen/image didn't actually attach
            self.ui.put(("error", f"{failed} image(s) couldn't be read and were not sent."))
        if not content:
            return text
        msg = {"type": "user",
               "message": {"role": "user", "content": content},
               "parent_tool_use_id": None}

        async def _one():
            yield msg

        return _one()

    @staticmethod
    def _msg_has_tool(msg):
        """True if this stream message starts/contains a tool_use — used to extend the
        receive idle budget so a long, silent tool isn't mistaken for a dead transport."""
        try:
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "content_block_start":
                    return ((ev.get("content_block") or {}).get("type") == "tool_use")
            elif isinstance(msg, AssistantMessage):
                return any(isinstance(b, ToolUseBlock)
                           for b in (getattr(msg, "content", None) or []))
        except Exception:
            pass
        return False

    def _dispatch(self, msg, blocks: dict):
        # The contents are untrusted CLI stream-json — a single malformed frame
        # (non-dict block value, unhashable index, content=None, …) must never abort
        # the turn (which would also skip the reconnect logic). Skip the bad frame and
        # keep streaming.
        try:
            self._dispatch_inner(msg, blocks)
        except Exception:
            pass

    def _dispatch_inner(self, msg, blocks: dict):
        if isinstance(msg, StreamEvent):
            self._saw_stream = True
            ev = msg.event or {}
            t = ev.get("type")
            if t == "content_block_start":
                idx = ev.get("index")
                cb = ev.get("content_block", {}) or {}
                blocks[idx] = {"type": cb.get("type"), "name": cb.get("name"), "buf": ""}
            elif t == "content_block_delta":
                idx = ev.get("index")
                d = ev.get("delta", {}) or {}
                dt = d.get("type")
                if dt == "text_delta":
                    self.ui.put(("delta", d.get("text", "")))
                elif dt == "thinking_delta":   # extended-thinking tokens (stream them so the
                    self.ui.put(("think", d.get("thinking", "")))   # pre-answer wait looks alive
                elif dt == "input_json_delta":
                    b = blocks.get(idx)
                    if not isinstance(b, dict):   # corrupted/missing → reset to a fresh buf
                        b = {"type": "tool_use", "name": None, "buf": ""}
                        blocks[idx] = b
                    b["buf"] = (b.get("buf") or "") + (d.get("partial_json") or "")
            elif t == "content_block_stop":
                idx = ev.get("index")
                b = blocks.get(idx)
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    try:
                        inp = json.loads(b.get("buf") or "{}")
                    except Exception:
                        inp = {}
                    self.ui.put(("tool", (b.get("name") or "tool", inp)))
        elif isinstance(msg, AssistantMessage):
            if getattr(msg, "model", None):
                # msg.model is the authoritative served model but drops the [1m] suffix, so
                # reconcile it with the id we requested (keeps the [1m] badge, and still shows
                # a real cross-family override). See _display_model.
                self.ui.put(("model", self._display_model(served=msg.model)))
            if not self._saw_stream:
                for blk in (getattr(msg, "content", None) or []):
                    if isinstance(blk, TextBlock):
                        self.ui.put(("delta", blk.text))
                    elif isinstance(blk, ToolUseBlock):
                        self.ui.put(("tool", (blk.name, blk.input)))
        elif isinstance(msg, ResultMessage):
            is_err = getattr(msg, "is_error", False)
            subtype = getattr(msg, "subtype", None)
            detail = getattr(msg, "result", None)
            stop_reason = getattr(msg, "stop_reason", None)
            if is_err:   # log the REAL reason (error detail, not reply text) so a past
                         # occurrence is diagnosable from the activity log
                dbg("result_error", "subtype=%s stop=%s detail=%s"
                    % (subtype, stop_reason, str(detail)[:300]))
            self.ui.put(("result", {"cost": getattr(msg, "total_cost_usd", None),
                                    "is_error": is_err, "subtype": subtype,
                                    "result": detail, "stop_reason": stop_reason}))
